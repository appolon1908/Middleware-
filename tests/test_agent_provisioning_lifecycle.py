"""Real-database lifecycle regressions for the agent provisioning saga.

Covers deprovisioning (suspend/revoke), reactivation, exclusive agent/user
bindings, provider error handling, step-level crash recovery and tenant
isolation. Every provider is a local fake; no live system is contacted.
Runs only against a disposable PostgreSQL (see tests/test_agent_provisioning.py).
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.adapters.keycloak.lifecycle_client import KeycloakLifecycleAdapter
from app.adapters.vicidial.mtls_client import VicidialMtlsError
from app.api.v1 import agent_provisioning
from app.core.config import settings
from tests import test_agent_provisioning as _base
from tests.test_agent_provisioning import _body, _headers, _internal_id_for

# Shared pytest fixtures, bound by attribute so test parameters may use them.
authority = _base.authority
client = _base.client

pytestmark = pytest.mark.skipif(
    "DATABASE_URL" not in os.environ, reason="disposable PostgreSQL required"
)

BASE = "/platform/v1/agent-provisioning/requests"


@dataclass(frozen=True)
class _KeycloakRecord:
    keycloak_subject: str
    enabled: bool = True


def _subject_for(email: str) -> str:
    return "kc-" + hashlib.sha256(email.encode()).hexdigest()[:16]


def _open_switches(monkeypatch) -> None:
    for key in ("live_identity_provisioning_enabled", "vicidial_write_enabled", "live_writes_enabled"):
        monkeypatch.setattr(settings, key, True)


def _keycloak(monkeypatch, *, shared_subject: str | None = None) -> list[tuple[str, str]]:
    """Fake Keycloak: one stable subject per email (or one shared subject)."""
    calls: list[tuple[str, str]] = []

    async def query_user_by_email(self, email):
        return _KeycloakRecord(shared_subject or _subject_for(email))

    async def create_user(self, email, first_name, last_name):
        return _KeycloakRecord(shared_subject or _subject_for(email))

    async def assign_approved_roles(self, subject, roles):
        return None

    async def disable_user(self, subject):
        calls.append(("disable_user", subject))

    async def enable_user(self, subject):
        calls.append(("enable_user", subject))

    for name, value in {
        "query_user_by_email": query_user_by_email, "create_user": create_user,
        "assign_approved_roles": assign_approved_roles, "disable_user": disable_user,
        "enable_user": enable_user,
    }.items():
        monkeypatch.setattr(KeycloakLifecycleAdapter, name, value)
    return calls


def _vicidial(monkeypatch, *, reserved_extension: str = "6203") -> SimpleNamespace:
    """Fake Vicidialer-Codestra edge. ``state.failures[op]`` raises once set."""
    state = SimpleNamespace(calls=[], payloads={}, failures={}, responses={})

    class _FakeVicidialClient:
        def __init__(self, _settings):
            pass

        def _call(self, operation, payload, kwargs, default):
            assert kwargs["correlation_id"]
            assert kwargs["request_id"].endswith(f":{operation}")
            state.calls.append(operation)
            state.payloads.setdefault(operation, []).append(payload)
            if operation in state.failures:
                raise state.failures[operation]
            return state.responses.get(operation, default)

        def sync_agent(self, payload, **kwargs):
            return self._call("sync_agent", payload, kwargs, {
                "actual": {"user_id": payload["agent"]["user_id"], "active": False},
            })

        def reserve_extension(self, payload, **kwargs):
            return self._call("reserve_extension", payload, kwargs, {
                "actual": {"extension": reserved_extension},
            })

        def adopt_extension(self, payload, **kwargs):
            return self._call("adopt_extension", payload, kwargs, {
                "actual": {"extension": payload["adoption"]["extension"]},
            })

        def provision_webrtc(self, payload, **kwargs):
            return self._call("provision_webrtc", payload, kwargs, {
                "credential": "synthetic-one-time-secret", "expires_at": "later",
            })

        def revoke_webrtc(self, payload, **kwargs):
            return self._call("revoke_webrtc", payload, kwargs, {"revoked": True})

        def disable_agent(self, payload, **kwargs):
            return self._call("disable_agent", payload, kwargs, {"actual": {"active": False}})

        def close(self):
            pass

    monkeypatch.setattr(agent_provisioning, "VicidialMtlsClient", _FakeVicidialClient)
    return state


def _telephony_body(
    *, employee_id: str = "COD.2026.00016", user_id: str = "COD0016",
    email: str = "agent.0016@example.invalid", extension: str | None = None,
    webrtc: bool = True,
) -> dict:
    return _body(
        employee_id=employee_id,
        identity={"email": email, "first_name": "Synthetic", "last_name": "Agent"},
        channels={"odoo": True, "phone": True, "webrtc": webrtc, "sms": False, "email": False},
        campaigns=[{
            "campaign_id": "TEST_SYN", "role": "supervisor",
            "vicidial_user_id": user_id, "vicidial_user_group": "COD_TEST_SDR",
            "vicidial_supervisor_subject": "supervisor-cod",
        }],
        telephony={
            "existing_extension": extension,
            "extension_pool": None if extension else "moneybee",
            "incoming_allowed": True, "outgoing_allowed": True, "max_webrtc_sessions": 1,
        },
    )


def _transient() -> VicidialMtlsError:
    request = httpx.Request("POST", "https://edge.internal.codestra.agency:8443/v1/x")
    try:
        raise VicidialMtlsError("VICidial private request failed closed") from (
            httpx.ConnectTimeout("timeout", request=request)
        )
    except VicidialMtlsError as exc:
        return exc


def _http_error(status: int) -> VicidialMtlsError:
    request = httpx.Request("POST", "https://edge.internal.codestra.agency:8443/v1/x")
    cause = httpx.HTTPStatusError(
        "status", request=request, response=httpx.Response(status, request=request),
    )
    try:
        raise VicidialMtlsError("VICidial private request failed closed") from cause
    except VicidialMtlsError as exc:
        return exc


def _steps(payload: dict, operation: str) -> list[dict]:
    return [step for step in payload["steps"] if step["operation"] == operation]


async def _create(client, token, body, **header_overrides):
    response = await client.post(BASE, json=body, headers=_headers(token, **header_overrides))
    assert response.status_code == 202, response.text
    return response.json()


async def _act(client, token, request_pk: str, action: str):
    return await client.post(
        f"{BASE}/{request_pk}/{action}", json={"reason": f"synthetic {action}"},
        headers=_headers(token),
    )


async def _sql(statement: str, **params):
    engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            return (await connection.execute(text(statement), params)).all() \
                if statement.lstrip().upper().startswith("SELECT") \
                else (await connection.execute(text(statement), params))
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_reconcile_never_resurrects_a_suspended_agent(client, authority):
    token = authority()
    body = _body()
    await _create(client, token, body)
    request_pk = await _internal_id_for(body["request_id"])
    assert (await _act(client, token, request_pk, "suspend")).json()["state"] == "SUSPENDED"

    refused = await _act(client, token, request_pk, "reconcile")
    assert refused.status_code == 409
    current = await client.get(f"{BASE}/{request_pk}", headers={"Authorization": f"Bearer {token}"})
    assert current.json()["state"] == "SUSPENDED"


@pytest.mark.asyncio
async def test_revoke_deprovisions_every_live_effect_and_is_idempotent(
    client, authority, monkeypatch,
):
    _open_switches(monkeypatch)
    keycloak_calls = _keycloak(monkeypatch)
    vicidial = _vicidial(monkeypatch)
    token = authority()
    body = _telephony_body()

    created = await _create(client, token, body)
    assert created["state"] == "EFFECTIVE", created["steps"]
    assert created["binding"] == {
        "keycloak_subject": _subject_for("agent.0016@example.invalid"),
        "vicidial_user_id": "COD0016", "agent_state": "synced",
        "extension": "6203", "extension_mode": "reserved", "webrtc_state": "provisioned",
    }
    assert "synthetic-one-time-secret" not in str(created)

    vicidial.calls.clear()
    request_pk = await _internal_id_for(body["request_id"])
    revoked = await _act(client, token, request_pk, "revoke")
    assert revoked.status_code == 200
    payload = revoked.json()
    assert payload["state"] == "REVOKED"
    assert payload["last_error_code"] is None
    assert vicidial.calls == ["revoke_webrtc", "disable_agent"]
    assert vicidial.payloads["disable_agent"][0]["user_id"] == "COD0016"
    assert keycloak_calls == [("disable_user", created["keycloak_subject"])]
    assert payload["binding"]["agent_state"] == "disabled"
    assert payload["binding"]["webrtc_state"] == "revoked"

    again = await _act(client, token, request_pk, "revoke")
    assert again.status_code == 200
    assert again.json()["version"] == payload["version"]
    assert len(again.json()["steps"]) == len(payload["steps"])
    assert vicidial.calls == ["revoke_webrtc", "disable_agent"]
    assert len(keycloak_calls) == 1

    events = await _sql(
        "SELECT topic FROM outbox_event WHERE payload->>'request_id' = :rid "
        "AND topic = 'platform.user.revoked'",
        rid=body["request_id"],
    )
    assert len(events) == 1


@pytest.mark.asyncio
async def test_failed_suspend_is_visible_and_retry_reissues_only_what_is_still_live(
    client, authority, monkeypatch,
):
    _open_switches(monkeypatch)
    keycloak_calls = _keycloak(monkeypatch)
    vicidial = _vicidial(monkeypatch)
    token = authority()
    body = _telephony_body()
    await _create(client, token, body)
    request_pk = await _internal_id_for(body["request_id"])

    vicidial.calls.clear()
    vicidial.failures["disable_agent"] = _transient()
    first = (await _act(client, token, request_pk, "suspend")).json()
    assert first["state"] == "SUSPENDED"
    assert first["last_error_code"] == "DEPROVISION_INCOMPLETE"
    failed = _steps(first, "disable_agent")[-1]
    assert failed["state"] == "failed"
    assert failed["error_code"] == "VICIDIAL_UNAVAILABLE"
    assert failed["retryable"] is True
    assert _steps(first, "revoke_webrtc")[-1]["state"] == "succeeded"
    assert len(keycloak_calls) == 1

    vicidial.calls.clear()
    del vicidial.failures["disable_agent"]
    second = (await _act(client, token, request_pk, "suspend")).json()
    assert second["state"] == "SUSPENDED"
    assert second["last_error_code"] is None
    assert vicidial.calls == ["disable_agent"]
    assert [step["attempt"] for step in _steps(second, "disable_agent")] == [1, 2]
    assert len(keycloak_calls) == 1


@pytest.mark.asyncio
async def test_reactivate_reenables_identity_and_reissues_only_undone_effects(
    client, authority, monkeypatch,
):
    _open_switches(monkeypatch)
    keycloak_calls = _keycloak(monkeypatch)
    vicidial = _vicidial(monkeypatch)
    token = authority()
    body = _telephony_body()
    await _create(client, token, body)
    request_pk = await _internal_id_for(body["request_id"])
    await _act(client, token, request_pk, "suspend")

    vicidial.calls.clear()
    reactivated = await _act(client, token, request_pk, "reactivate")
    assert reactivated.status_code == 200
    payload = reactivated.json()
    assert payload["state"] == "EFFECTIVE", payload["steps"]
    assert keycloak_calls[-1][0] == "enable_user"
    # The extension stays bound; only the agent and WebRTC were undone.
    assert vicidial.calls == ["sync_agent", "provision_webrtc"]
    assert vicidial.payloads["sync_agent"][-1]["context"]["expected_version"] == 2
    assert vicidial.payloads["provision_webrtc"][-1]["context"]["expected_version"] == 2
    assert payload["binding"]["agent_state"] == "synced"
    assert payload["binding"]["webrtc_state"] == "provisioned"

    suspended_again = (await _act(client, token, request_pk, "suspend")).json()
    assert suspended_again["last_error_code"] is None
    assert [name for name, _ in keycloak_calls] == ["disable_user", "enable_user", "disable_user"]


@pytest.mark.asyncio
async def test_extension_bound_to_another_employee_is_never_adopted(
    client, authority, monkeypatch,
):
    _open_switches(monkeypatch)
    _keycloak(monkeypatch)
    vicidial = _vicidial(monkeypatch)
    token = authority()
    first_body = _telephony_body(extension="6101")
    assert (await _create(client, token, first_body))["state"] == "EFFECTIVE"

    second_body = _telephony_body(
        employee_id="COD.2026.00099", user_id="COD0099",
        email="agent.0099@example.invalid", extension="6101",
    )
    second = await _create(client, token, second_body)
    assert second["state"] == "FAILED"
    conflict = _steps(second, "adopt_extension")[-1]
    assert conflict["error_code"] == "EXTENSION_BINDING_CONFLICT"
    assert conflict["retryable"] is False
    assert vicidial.calls.count("adopt_extension") == 1

    # Once the first employee is cleanly revoked the extension is free.
    first_pk = await _internal_id_for(first_body["request_id"])
    assert (await _act(client, token, first_pk, "revoke")).json()["last_error_code"] is None
    second_pk = await _internal_id_for(second_body["request_id"])
    vicidial.calls.clear()
    retried = (await _act(client, token, second_pk, "reconcile")).json()
    assert retried["state"] == "EFFECTIVE", retried["steps"]
    assert vicidial.calls == ["adopt_extension", "provision_webrtc"]
    assert retried["binding"]["extension_mode"] == "adopted"


@pytest.mark.asyncio
async def test_vicidial_user_bound_to_another_employee_is_refused(
    client, authority, monkeypatch,
):
    _open_switches(monkeypatch)
    _keycloak(monkeypatch)
    vicidial = _vicidial(monkeypatch)
    token = authority()
    assert (await _create(client, token, _telephony_body()))["state"] == "EFFECTIVE"

    other = await _create(client, token, _telephony_body(
        employee_id="COD.2026.00098", email="agent.0098@example.invalid",
    ))
    assert other["state"] == "FAILED"
    assert _steps(other, "sync_agent")[-1]["error_code"] == "AGENT_BINDING_CONFLICT"
    assert vicidial.calls.count("sync_agent") == 1


@pytest.mark.asyncio
async def test_keycloak_identity_bound_to_another_employee_is_refused(
    client, authority, monkeypatch,
):
    _open_switches(monkeypatch)
    _keycloak(monkeypatch, shared_subject="kc-shared-subject")
    token = authority()
    odoo_only = {"odoo": True, "phone": False, "webrtc": False, "sms": False, "email": False}
    first = await _create(client, token, _body(channels=odoo_only))
    assert first["keycloak_subject"] == "kc-shared-subject"

    second = await _create(client, token, _body(employee_id="COD.2026.00097", channels=odoo_only))
    assert second["state"] == "FAILED"
    assert second["keycloak_subject"] is None
    assert _steps(second, "create_user")[-1]["error_code"] == "IDENTITY_BINDING_CONFLICT"


@pytest.mark.asyncio
async def test_unconfigured_vicidial_transport_fails_the_saga_instead_of_crashing(
    client, authority, monkeypatch,
):
    _open_switches(monkeypatch)
    _keycloak(monkeypatch)
    monkeypatch.setattr(settings, "vicidial_ca_file", "")
    token = authority()
    payload = await _create(client, token, _telephony_body())
    assert payload["state"] == "FAILED"
    assert _steps(payload, "provision_phone")[-1]["error_code"] == "VICIDIAL_NOT_CONFIGURED"


@pytest.mark.asyncio
async def test_webrtc_session_already_active_is_provisioned_not_failed(
    client, authority, monkeypatch,
):
    _open_switches(monkeypatch)
    _keycloak(monkeypatch)
    vicidial = _vicidial(monkeypatch)
    vicidial.failures["provision_webrtc"] = _http_error(409)
    token = authority()
    payload = await _create(client, token, _telephony_body())
    assert payload["state"] == "EFFECTIVE", payload["steps"]
    assert _steps(payload, "provision_webrtc")[-1]["readback_state"] == "webrtc_session_active"


@pytest.mark.asyncio
async def test_extension_conflict_from_the_provider_is_not_retryable(
    client, authority, monkeypatch,
):
    _open_switches(monkeypatch)
    _keycloak(monkeypatch)
    vicidial = _vicidial(monkeypatch)
    vicidial.failures["adopt_extension"] = _http_error(409)
    token = authority()
    payload = await _create(client, token, _telephony_body(extension="6102"))
    assert payload["state"] == "FAILED"
    step = _steps(payload, "adopt_extension")[-1]
    assert step["error_code"] == "EXTENSION_CONFLICT"
    assert step["retryable"] is False


@pytest.mark.asyncio
async def test_unconfirmed_reservation_is_a_readback_mismatch_until_reconciled(
    client, authority, monkeypatch,
):
    _open_switches(monkeypatch)
    _keycloak(monkeypatch)
    vicidial = _vicidial(monkeypatch)
    vicidial.responses["reserve_extension"] = {"actual": {}}
    token = authority()
    body = _telephony_body()
    first = await _create(client, token, body)
    assert first["state"] == "FAILED"
    assert _steps(first, "reserve_extension")[-1]["error_code"] == "READBACK_MISMATCH"
    assert first["binding"]["extension"] is None

    del vicidial.responses["reserve_extension"]
    vicidial.calls.clear()
    request_pk = await _internal_id_for(body["request_id"])
    second = (await _act(client, token, request_pk, "reconcile")).json()
    assert second["state"] == "EFFECTIVE", second["steps"]
    assert vicidial.calls == ["reserve_extension", "provision_webrtc"]
    assert [s["attempt"] for s in _steps(second, "reserve_extension")] == [1, 2]
    assert second["binding"]["extension"] == "6203"


async def _simulate_crash(request_id: str, *, state: str, age_seconds: int) -> None:
    """Leave a row exactly as a runner that died mid-saga would."""
    await _sql(
        "DELETE FROM idempotency_record WHERE scope = 'agent_provisioning' "
        "AND key_hash = (SELECT idempotency_hash FROM agent_provisioning_request "
        "WHERE request_id = :rid)",
        rid=request_id,
    )
    await _sql(
        "UPDATE agent_provisioning_request SET state = :state, "
        "updated_at = now() - make_interval(secs => :age) WHERE request_id = :rid",
        state=state, age=age_seconds, rid=request_id,
    )


@pytest.mark.asyncio
async def test_same_key_retry_resumes_a_crashed_saga_but_not_a_running_one(
    client, authority,
):
    token = authority()
    body = _body()
    key = uuid4().hex
    await _create(client, token, body, idempotency_key=key)

    await _simulate_crash(body["request_id"], state="CHANNEL_PROVISIONING", age_seconds=5)
    running = await client.post(BASE, json=body, headers=_headers(token, idempotency_key=key))
    assert running.status_code == 409

    await _simulate_crash(body["request_id"], state="CHANNEL_PROVISIONING", age_seconds=3600)
    resumed = await client.post(BASE, json=body, headers=_headers(token, idempotency_key=key))
    assert resumed.status_code == 202
    assert resumed.json()["state"] == "PARTIAL"
    replay = await client.post(BASE, json=body, headers=_headers(token, idempotency_key=key))
    assert replay.json() == resumed.json()

    audits = await _sql(
        "SELECT a.action, a.from_state FROM agent_provisioning_audit a "
        "JOIN agent_provisioning_request r ON r.id = a.request_id "
        "WHERE r.request_id = :rid AND a.action = 'recover'",
        rid=body["request_id"],
    )
    assert [(row.action, row.from_state) for row in audits] == [("recover", "CHANNEL_PROVISIONING")]


@pytest.mark.asyncio
async def test_reconciler_resumes_only_abandoned_sagas(client, authority):
    token = authority()
    stale_body = _body()
    fresh_body = _body(employee_id="COD.2026.00096")
    await _create(client, token, stale_body)
    await _create(client, token, fresh_body)
    await _simulate_crash(stale_body["request_id"], state="IDENTITY", age_seconds=3600)
    await _simulate_crash(fresh_body["request_id"], state="IDENTITY", age_seconds=1)

    engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=NullPool)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        resumed = await agent_provisioning.resume_stale_sagas(factory, limit=10)
    finally:
        await engine.dispose()
    assert resumed == [stale_body["request_id"]]

    rows = await _sql(
        "SELECT request_id, state FROM agent_provisioning_request "
        "WHERE request_id IN (:a, :b)",
        a=stale_body["request_id"], b=fresh_body["request_id"],
    )
    states = {row.request_id: row.state for row in rows}
    assert states == {stale_body["request_id"]: "PARTIAL", fresh_body["request_id"]: "IDENTITY"}


@pytest.mark.asyncio
async def test_other_tenants_cannot_probe_or_mutate_a_request(client, authority):
    owner = authority()
    body = _body()
    await _create(client, owner, body)
    request_pk = await _internal_id_for(body["request_id"])

    outsider = authority(tenant_ids=("OTHER_TENANT",))
    probe = await client.get(f"{BASE}/{request_pk}", headers={"Authorization": f"Bearer {outsider}"})
    assert probe.status_code == 404
    missing = await client.get(f"{BASE}/{uuid4()}", headers={"Authorization": f"Bearer {outsider}"})
    assert missing.status_code == 404
    assert (await _act(client, outsider, request_pk, "revoke")).status_code == 404


@pytest.mark.asyncio
async def test_stale_policy_revision_blocks_lifecycle_actions(client, authority):
    token = authority()
    body = _body()
    await _create(client, token, body)
    request_pk = await _internal_id_for(body["request_id"])
    response = await client.post(
        f"{BASE}/{request_pk}/suspend", json={"reason": "stale"},
        headers=_headers(token, policy_revision="stale-revision"),
    )
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_same_key_retry_replays_a_response_stored_while_it_waited(
    client, authority, monkeypatch,
):
    """The first attempt finished between this retry's idempotency lookup
    and its row lock: the retry must replay, not collide on the record."""
    token = authority()
    body = _body()
    key = uuid4().hex
    first = await _create(client, token, body, idempotency_key=key)

    real_lookup = agent_provisioning._stored_response
    lookups = {"count": 0}

    async def lookup_that_misses_once(*args, **kwargs):
        lookups["count"] += 1
        if lookups["count"] == 1:
            return None
        return await real_lookup(*args, **kwargs)

    monkeypatch.setattr(agent_provisioning, "_stored_response", lookup_that_misses_once)
    retry = await client.post(BASE, json=body, headers=_headers(token, idempotency_key=key))
    assert retry.status_code == 202, retry.text
    assert retry.json() == first
    assert lookups["count"] == 2

    records = await _sql(
        "SELECT count(*) AS n FROM idempotency_record WHERE scope = 'agent_provisioning' "
        "AND key_hash = (SELECT idempotency_hash FROM agent_provisioning_request "
        "WHERE request_id = :rid)",
        rid=body["request_id"],
    )
    assert records[0].n == 1


@pytest.mark.asyncio
async def test_request_id_reused_under_a_new_key_is_refused_without_side_effects(
    client, authority,
):
    token = authority()
    body = _body()
    await _create(client, token, body)
    duplicate = await client.post(BASE, json=body, headers=_headers(token))
    assert duplicate.status_code == 409
    rows = await _sql(
        "SELECT count(*) AS n FROM agent_provisioning_request WHERE request_id = :rid",
        rid=body["request_id"],
    )
    assert rows[0].n == 1


@pytest.mark.asyncio
async def test_revoke_after_a_clean_suspend_withdraws_without_new_provider_calls(
    client, authority, monkeypatch,
):
    _open_switches(monkeypatch)
    keycloak_calls = _keycloak(monkeypatch)
    vicidial = _vicidial(monkeypatch)
    token = authority()
    body = _telephony_body()
    await _create(client, token, body)
    request_pk = await _internal_id_for(body["request_id"])
    suspended = (await _act(client, token, request_pk, "suspend")).json()
    assert suspended["last_error_code"] is None

    vicidial.calls.clear()
    revoked = await _act(client, token, request_pk, "revoke")
    assert revoked.status_code == 200
    payload = revoked.json()
    assert payload["state"] == "REVOKED"
    assert payload["last_error_code"] is None
    assert payload["version"] == suspended["version"] + 1
    assert len(payload["steps"]) == len(suspended["steps"])
    assert vicidial.calls == []
    assert [name for name, _ in keycloak_calls] == ["disable_user"]
    # Revoked is final: neither reactivate nor reconcile can bring it back.
    assert (await _act(client, token, request_pk, "reactivate")).status_code == 409
    assert (await _act(client, token, request_pk, "reconcile")).status_code == 409


@pytest.mark.asyncio
async def test_reconciler_skips_a_poisoned_saga_and_resumes_the_rest(
    client, authority, monkeypatch,
):
    token = authority()
    poisoned_body = _body()
    healthy_body = _body(employee_id="COD.2026.00097")
    await _create(client, token, poisoned_body)
    await _create(client, token, healthy_body)
    # The poisoned row is the oldest, so it is claimed first.
    await _simulate_crash(poisoned_body["request_id"], state="IDENTITY", age_seconds=7200)
    await _simulate_crash(healthy_body["request_id"], state="IDENTITY", age_seconds=3600)

    real_advance = agent_provisioning._advance_saga

    async def advance(session, request, principal, **kwargs):
        if request.request_id == poisoned_body["request_id"]:
            raise RuntimeError("synthetic poisoned saga")
        return await real_advance(session, request, principal, **kwargs)

    monkeypatch.setattr(agent_provisioning, "_advance_saga", advance)
    engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=NullPool)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        resumed = await agent_provisioning.resume_stale_sagas(factory, limit=10)
    finally:
        await engine.dispose()
    assert resumed == [healthy_body["request_id"]]

    rows = await _sql(
        "SELECT request_id, state FROM agent_provisioning_request "
        "WHERE request_id IN (:a, :b)",
        a=poisoned_body["request_id"], b=healthy_body["request_id"],
    )
    states = {row.request_id: row.state for row in rows}
    # The poisoned row keeps its committed RECONCILING checkpoint and a fresh lease.
    assert states == {
        poisoned_body["request_id"]: "RECONCILING", healthy_body["request_id"]: "PARTIAL",
    }
