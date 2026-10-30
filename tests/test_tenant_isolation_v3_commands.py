"""Adversarial tenant isolation for the V3 command kernel (/platform/v1).

A principal of tenant B attacks an operation owned by tenant A through every
read and mutation surface of the canonical integration application. Each
attempt must be denied without disclosing A's data (404 when B names its own
tenant, 403 when B names A's tenant in ``X-Tenant-ID``) and A's operation,
timeline, attempts, outbox intents and provider effects must be unchanged.

Both tenants are registered as synthetic tenants so the TEST_SYN fixture
adapter may execute for either; nothing here reaches a provider, Postgres or
the network. Tokens are assembled at runtime (never literal JWTs) and
verified by the same claims verifier the platform API tests use.
"""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from app.application import AppProfile, create_app
from app.commands import (
    CommandConflict,
    CommandEnvelope,
    CommandNotFound,
    CommandService,
    MemoryCommandStore,
)
from app.core.config import Settings
from app.core.runtime import RuntimeContainer
from app.platform.adapter import AdapterContext
from app.platform.kernel import PolicyDenied
from app.platform.memory import MemoryExecutionBus
from app.platform.principal import KernelPrincipal
from app.platform.runtime import build_platform_runtime, command_policies
from app.platform.safety import SafetyGate, SafetySwitches
from app.replay import MemoryReplayGuard
from app.control_plane_auth import caller_for_client_id
from app.storage import MemoryInboxStore
from tests.test_platform_api import ClaimsVerifier, token

TENANT_A = "TEST_SYN"
TENANT_B = "TEST_SYN_TENANT_B"
SECRET_MARKER = "tenant-a-private-marker-7f3c"
READ = "platform.command platform.command.read"
OPERATOR_SCOPES = "platform.command platform.command.read platform.command.replay"
OPERATOR_ROLES = ("platform-operator",)


class Stack:
    def __init__(self, settings: Settings) -> None:
        self.store = MemoryCommandStore()
        commands = CommandService(store=self.store, policies=command_policies(settings))
        switches = SafetySwitches.load()
        switches = dataclasses.replace(
            switches, synthetic_tenants=frozenset({TENANT_A, TENANT_B})
        )
        safety = SafetyGate(settings, commands.policies, switches)
        self.runtime = RuntimeContainer(
            settings=settings,
            inbox=MemoryInboxStore(),
            replay=MemoryReplayGuard(),
            tokens=ClaimsVerifier(),
            commands=commands,
        )
        self.runtime.platform = build_platform_runtime(
            settings,
            commands=commands,
            http=None,
            pool=None,
            service_id="middleware-integration-api",
            safety=safety,
        )
        self.bus = MemoryExecutionBus(self.store, self.runtime.platform.dispatch)
        self.app = create_app(
            settings=settings, runtime=self.runtime, profile=AppProfile.INTEGRATION
        )
        self.adapter = self.runtime.platform.registry.adapter("test-syn")
        self.dispatched: list[tuple[str, str]] = []
        original_execute = self.adapter.execute

        async def recording_execute(command: CommandEnvelope, context: AdapterContext):
            self.dispatched.append((str(command.command_id), context.tenant_id))
            return await original_execute(command, context)

        self.adapter.execute = recording_execute  # type: ignore[method-assign]

    def run_bus(self) -> None:
        asyncio.run(self.bus.run_once())

    def snapshot(self, tenant_id: str, command_id: str) -> dict[str, Any]:
        operation_id = UUID(command_id)
        operation = asyncio.run(self.store.get(tenant_id, operation_id))
        events = asyncio.run(self.store.list_events(tenant_id, operation_id, limit=500))
        attempts = asyncio.run(
            self.store.list_attempts(tenant_id, operation_id, limit=500)
        )
        return {
            "state": operation.state,
            "resource_version": operation.resource_version,
            "updated_at": operation.updated_at,
            "events": [event.event_id for event in events],
            "attempts": len(attempts),
            "outbox": len(self.store._outbox),
            "replays": len(self.store._replays),
            "effects": self.adapter.provider_effects,
        }


@pytest.fixture
def stack(test_settings: Settings) -> Stack:
    return Stack(test_settings)


def bearer(
    *,
    tenants: tuple[str, ...],
    scope: str = READ,
    sub: str = "user-1",
    roles: tuple[str, ...] = (),
) -> str:
    return f"Bearer {token(scope=scope, sub=sub, tenants=tenants, roles=roles)}"


def command_body(tenant_id: str, **updates: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "command_id": str(uuid4()),
        "command_type": "test.syn.execute.v1",
        "command_version": "1.0",
        "target": "test-syn",
        "tenant_id": tenant_id,
        "requested_by": "user-1",
        "correlation_id": "corr-" + uuid4().hex[:12],
        "idempotency_key": "idem-" + uuid4().hex,
        "capability": "TEST_SYN_EXECUTE",
        "payload": {"probe": SECRET_MARKER},
    }
    value.update(updates)
    return value


def submit(
    client: TestClient, body: dict[str, Any], *, authorization: str, **extra: str
):
    headers = {
        "Authorization": authorization,
        "X-Correlation-ID": body["correlation_id"],
        "Idempotency-Key": body["idempotency_key"],
        **extra,
    }
    return client.post("/platform/v1/commands", json=body, headers=headers)


def submit_a(client: TestClient, **updates: Any) -> dict[str, Any]:
    body = command_body(TENANT_A, **updates)
    response = submit(client, body, authorization=bearer(tenants=(TENANT_A,)))
    assert response.status_code == 202, response.text
    return body


def assert_non_disclosing(response, owner: dict[str, Any], *, status: int) -> None:
    assert response.status_code == status, response.text
    text = response.text
    assert owner["correlation_id"] not in text
    assert owner["idempotency_key"] not in text
    assert SECRET_MARKER not in text
    assert '"resource_version"' not in text and '"state"' not in text
    error = response.json()["error"]
    assert error["code"] == (
        "command_not_found" if status == 404 else "authorization_denied"
    )


def mutation_headers(
    authorization: str, *, key: str, tenant_header: str | None = None
) -> dict[str, str]:
    headers = {
        "Authorization": authorization,
        "X-Correlation-ID": "attacker-corr",
        "Idempotency-Key": key,
    }
    if tenant_header is not None:
        headers["X-Tenant-ID"] = tenant_header
    return headers


# ----------------------------------------------------------------------------
# reads: operation, result, timeline/history, attempts, recovery, dead letter
# ----------------------------------------------------------------------------
# (path, fixture behaviour, dead-letter the operation): each arrangement makes
# the path readable by tenant A so the denial for tenant B is meaningful.
READ_PATHS = (
    ("/platform/v1/operations/{id}", "reject", False),
    ("/platform/v1/commands/{id}", "reject", False),
    ("/platform/v1/commands/{id}/result", "reject", False),
    ("/platform/v1/operations/{id}/timeline", "reject", False),
    ("/platform/v1/commands/{id}/history", "reject", False),
    ("/platform/v1/operations/{id}/recovery-history", "reject", True),
    ("/platform/v1/dead-letters/{id}", "reject", True),
    ("/platform/v1/reconciliation/{id}", "unknown", False),
)


@pytest.mark.parametrize(
    "path, behaviour, dead_letter", READ_PATHS, ids=[item[0] for item in READ_PATHS]
)
def test_foreign_tenant_reads_are_non_disclosing(
    stack: Stack, path: str, behaviour: str, dead_letter: bool
) -> None:
    with TestClient(stack.app) as client:
        owner = submit_a(client, payload={"fixture": behaviour, "probe": SECRET_MARKER})
        stack.run_bus()
        if dead_letter:
            asyncio.run(
                stack.store.record_dead_letter(
                    TENANT_A,
                    UUID(owner["command_id"]),
                    actor_id="test",
                    reason_code="fixture_rejected",
                    error_class="NON_RETRYABLE",
                    terminal_reason="arranged",
                )
            )
        before = stack.snapshot(TENANT_A, owner["command_id"])
        url = path.format(id=owner["command_id"])

        own_tenant = client.get(
            url,
            headers={
                "Authorization": bearer(tenants=(TENANT_B,), scope=OPERATOR_SCOPES)
            },
        )
        assert_non_disclosing(own_tenant, owner, status=404)
        named_victim = client.get(
            url,
            headers={
                "Authorization": bearer(tenants=(TENANT_B,), scope=OPERATOR_SCOPES),
                "X-Tenant-ID": TENANT_A,
            },
        )
        assert_non_disclosing(named_victim, owner, status=403)
        multi_tenant = client.get(
            url,
            headers={
                "Authorization": bearer(
                    tenants=(TENANT_B, "TEST_SYN_OTHER"), scope=OPERATOR_SCOPES
                ),
                "X-Tenant-ID": TENANT_B,
            },
        )
        assert_non_disclosing(multi_tenant, owner, status=404)

        legitimate = client.get(
            url,
            headers={
                "Authorization": bearer(tenants=(TENANT_A,), scope=OPERATOR_SCOPES)
            },
        )
        assert legitimate.status_code == 200, legitimate.text
        assert stack.snapshot(TENANT_A, owner["command_id"]) == before


def test_foreign_tenant_cannot_list_attempts_or_list_operations(stack: Stack) -> None:
    with TestClient(stack.app) as client:
        owner = submit_a(client)
        stack.run_bus()
        operation_id = UUID(owner["command_id"])
        kernel = stack.runtime.platform.kernel
        assert len(asyncio.run(kernel.attempts(TENANT_A, operation_id))) >= 1
        with pytest.raises(CommandNotFound):
            asyncio.run(kernel.attempts(TENANT_B, operation_id))
        with pytest.raises(CommandNotFound):
            asyncio.run(kernel.timeline(TENANT_B, operation_id))
        with pytest.raises(CommandNotFound):
            asyncio.run(kernel.get(TENANT_B, operation_id))
        assert asyncio.run(stack.store.list_operations(TENANT_B, limit=100)) == []
        foreign_list = client.get(
            "/platform/v1/reconciliation",
            headers={
                "Authorization": bearer(tenants=(TENANT_B,), scope=OPERATOR_SCOPES)
            },
        )
        assert foreign_list.status_code == 200 and foreign_list.json() == {"items": []}
        foreign_dead = client.get(
            "/platform/v1/dead-letters",
            headers={"Authorization": bearer(tenants=(TENANT_B,))},
        )
        assert foreign_dead.status_code == 200 and foreign_dead.json() == {"items": []}


# ----------------------------------------------------------------------------
# mutations: cancel, retry, replay REPROCESS / REEXECUTE, dead-letter replay,
# reconciliation readback/resolve, connector readback/reconcile
# ----------------------------------------------------------------------------
def _cancel(client: TestClient, op: str, version: int, headers: dict[str, str]):
    return client.post(
        f"/platform/v1/operations/{op}/cancel",
        json={"expected_version": version, "reason": "attack"},
        headers=headers,
    )


def _cancel_alias(client: TestClient, op: str, version: int, headers: dict[str, str]):
    return client.post(
        f"/platform/v1/commands/{op}/cancel",
        json={"expected_version": version, "reason": "attack"},
        headers=headers,
    )


def _retry(client: TestClient, op: str, version: int, headers: dict[str, str]):
    return client.post(
        f"/platform/v1/commands/{op}/retry",
        json={"expected_version": version, "reason": "attack"},
        headers=headers,
    )


def _reprocess(client: TestClient, op: str, version: int, headers: dict[str, str]):
    return client.post(
        f"/platform/v1/operations/{op}/replay",
        json={"mode": "REPROCESS", "expected_version": version, "reason": "attack"},
        headers=headers,
    )


def _reexecute(client: TestClient, op: str, version: int, headers: dict[str, str]):
    return client.post(
        f"/platform/v1/operations/{op}/replay",
        json={
            "mode": "REEXECUTE",
            "expected_version": version,
            "reason": "attack",
            "new_idempotency_key": "attacker-new-key-0001",
        },
        headers=headers,
    )


def _dead_letter_replay(
    client: TestClient, op: str, version: int, headers: dict[str, str]
):
    return client.post(
        f"/platform/v1/dead-letters/{op}/replay",
        json={
            "mode": "REEXECUTE",
            "expected_version": version,
            "reason": "attack",
            "new_idempotency_key": "attacker-new-key-0002",
        },
        headers=headers,
    )


def _reconcile_readback(
    client: TestClient, op: str, version: int, headers: dict[str, str]
):
    return client.post(
        f"/platform/v1/reconciliation/{op}/readback",
        json={"expected_version": version, "reason": "attack"},
        headers=headers,
    )


def _connector_readback(
    client: TestClient, op: str, version: int, headers: dict[str, str]
):
    return client.post(
        "/platform/v1/connectors/test-syn/readback",
        json={"operation_id": op},
        headers=headers,
    )


def _connector_reconcile(
    client: TestClient, op: str, version: int, headers: dict[str, str]
):
    return client.post(
        "/platform/v1/connectors/test-syn/reconcile",
        json={"operation_id": op},
        headers=headers,
    )


# (name, call, fixture behaviour, execute on the bus first, dead-letter the operation)
MUTATIONS = (
    ("cancel", _cancel, "success", False, False),
    ("cancel_alias", _cancel_alias, "success", False, False),
    ("retry", _retry, "reject", True, False),
    ("replay_reprocess", _reprocess, "unknown", True, False),
    ("replay_reexecute", _reexecute, "reject", True, True),
    ("dead_letter_replay", _dead_letter_replay, "reject", True, True),
    ("reconciliation_readback", _reconcile_readback, "unknown", True, False),
    ("connector_readback", _connector_readback, "success", True, False),
    ("connector_reconcile", _connector_reconcile, "unknown", True, False),
)


@pytest.mark.parametrize(
    "name, call, behaviour, execute, dead_letter",
    MUTATIONS,
    ids=[item[0] for item in MUTATIONS],
)
def test_foreign_tenant_mutations_are_denied_and_leave_the_operation_unchanged(
    stack: Stack, name: str, call, behaviour: str, execute: bool, dead_letter: bool
) -> None:
    with TestClient(stack.app) as client:
        owner = submit_a(client, payload={"fixture": behaviour, "probe": SECRET_MARKER})
        op = owner["command_id"]
        if execute:
            stack.run_bus()
        if dead_letter:
            asyncio.run(
                stack.store.record_dead_letter(
                    TENANT_A,
                    UUID(op),
                    actor_id="test",
                    reason_code="fixture_rejected",
                    error_class="NON_RETRYABLE",
                    terminal_reason="arranged",
                )
            )
        before = stack.snapshot(TENANT_A, op)
        version = before["resource_version"]
        attacker = bearer(
            tenants=(TENANT_B,),
            scope=OPERATOR_SCOPES,
            sub="tenant-b-operator",
            roles=OPERATOR_ROLES,
        )

        own_tenant = call(
            client, op, version, mutation_headers(attacker, key=f"attack-{name}-0001")
        )
        assert_non_disclosing(own_tenant, owner, status=404)
        named_victim = call(
            client,
            op,
            version,
            mutation_headers(
                attacker, key=f"attack-{name}-0002", tenant_header=TENANT_A
            ),
        )
        assert_non_disclosing(named_victim, owner, status=403)
        multi = bearer(
            tenants=(TENANT_B, "TEST_SYN_OTHER"),
            scope=OPERATOR_SCOPES,
            sub="tenant-b-operator",
            roles=OPERATOR_ROLES,
        )
        multi_tenant = call(
            client,
            op,
            version,
            mutation_headers(multi, key=f"attack-{name}-0003", tenant_header=TENANT_B),
        )
        assert_non_disclosing(multi_tenant, owner, status=404)

        assert stack.snapshot(TENANT_A, op) == before
        assert asyncio.run(stack.store.list_operations(TENANT_B, limit=100)) == []
        assert all(intent.tenant_id == TENANT_A for intent in stack.store._outbox)


def test_receipt_style_resolution_naming_a_foreign_provider_reference_is_denied(
    stack: Stack,
) -> None:
    """The only V3 path that applies provider outcome evidence to an operation
    is operator reconciliation resolution (there is no inbound provider
    receipt route for V3 operations). Tenant B must not be able to apply a
    resolution carrying A's provider reference to A's operation."""
    with TestClient(stack.app) as client:
        owner = submit_a(client, payload={"fixture": "unknown", "probe": SECRET_MARKER})
        op = owner["command_id"]
        stack.run_bus()
        before = stack.snapshot(TENANT_A, op)
        assert before["state"] == "reconciliation_required"
        provider_reference = f"test-syn:{op}:1"
        body = {
            "expected_version": before["resource_version"],
            "matched": True,
            "reason": "forged receipt",
            "provider_operation_id": provider_reference,
            "evidence": {
                "receipt": "delivered",
                "provider_message_id": provider_reference,
            },
        }
        attacker = bearer(
            tenants=(TENANT_B,),
            scope=OPERATOR_SCOPES,
            sub="tenant-b-operator",
            roles=OPERATOR_ROLES,
        )
        own_tenant = client.post(
            f"/platform/v1/reconciliation/{op}/resolve",
            json=body,
            headers=mutation_headers(attacker, key="attack-resolve-0001"),
        )
        assert_non_disclosing(own_tenant, owner, status=404)
        named_victim = client.post(
            f"/platform/v1/reconciliation/{op}/resolve",
            json=body,
            headers=mutation_headers(
                attacker, key="attack-resolve-0002", tenant_header=TENANT_A
            ),
        )
        assert_non_disclosing(named_victim, owner, status=403)
        assert stack.snapshot(TENANT_A, op) == before


# ----------------------------------------------------------------------------
# submission with a foreign tenant in the body or X-Tenant-ID
# ----------------------------------------------------------------------------
def test_submission_naming_a_foreign_tenant_is_denied_and_never_returns_the_victim_operation(
    stack: Stack,
) -> None:
    with TestClient(stack.app) as client:
        owner = submit_a(client)
        before = stack.snapshot(TENANT_A, owner["command_id"])
        attacker = bearer(tenants=(TENANT_B,))

        # exact resubmission of A's command (A's tenant, key and id) by B
        own_correlation = {"X-Correlation-ID": "attacker-corr"}
        replayed = submit(client, owner, authorization=attacker, **own_correlation)
        assert_non_disclosing(replayed, owner, status=403)
        replayed_with_header = submit(
            client,
            owner,
            authorization=attacker,
            **own_correlation,
            **{"X-Tenant-ID": TENANT_A},
        )
        assert_non_disclosing(replayed_with_header, owner, status=403)

        fresh_body_a = command_body(TENANT_A)
        assert submit(client, fresh_body_a, authorization=attacker).status_code == 403
        assert (
            submit(
                client,
                fresh_body_a,
                authorization=attacker,
                **{"X-Tenant-ID": TENANT_B},
            ).status_code
            == 403
        )

        # B's own tenant in the body with A's tenant in the header
        body_b = command_body(
            TENANT_B,
            command_id=owner["command_id"],
            idempotency_key=owner["idempotency_key"],
        )
        mismatch = submit(
            client, body_b, authorization=attacker, **{"X-Tenant-ID": TENANT_A}
        )
        assert mismatch.status_code == 400
        assert owner["correlation_id"] not in mismatch.text

        # forged identity headers do not widen the token
        forged = submit(
            client,
            fresh_body_a,
            authorization=attacker,
            **{"X-Forwarded-User": "admin", "X-Codestra-Tenant-Id": TENANT_A},
        )
        assert forged.status_code == 403

        assert stack.snapshot(TENANT_A, owner["command_id"]) == before
        assert asyncio.run(stack.store.list_operations(TENANT_B, limit=100)) == []


# ----------------------------------------------------------------------------
# idempotency is tenant-scoped
# ----------------------------------------------------------------------------
@pytest.mark.parametrize(
    "share_command_id", [False, True], ids=["distinct_command_ids", "same_command_id"]
)
def test_same_idempotency_key_and_payload_under_two_tenants_are_two_operations(
    stack: Stack, share_command_id: bool
) -> None:
    with TestClient(stack.app) as client:
        key = "idem-shared-" + uuid4().hex
        payload = {"probe": SECRET_MARKER}
        body_a = command_body(
            TENANT_A,
            idempotency_key=key,
            payload=payload,
            correlation_id="corr-tenant-a",
        )
        body_b = command_body(
            TENANT_B,
            idempotency_key=key,
            payload=payload,
            correlation_id="corr-tenant-b",
        )
        if share_command_id:
            body_b["command_id"] = body_a["command_id"]
        auth_a = bearer(tenants=(TENANT_A,))
        auth_b = bearer(tenants=(TENANT_B,))

        first_a = submit(client, body_a, authorization=auth_a)
        first_b = submit(client, body_b, authorization=auth_b)
        assert first_a.status_code == 202 and first_a.json()["duplicate"] is False
        assert first_b.status_code == 202 and first_b.json()["duplicate"] is False, (
            first_b.text
        )
        assert first_b.json()["correlation_id"] == "corr-tenant-b"

        replay_a = submit(client, body_a, authorization=auth_a)
        replay_b = submit(client, body_b, authorization=auth_b)
        assert (
            replay_a.status_code == 200
            and replay_a.json()["operation_id"] == body_a["command_id"]
        )
        assert replay_a.json()["correlation_id"] == "corr-tenant-a"
        assert (
            replay_b.status_code == 200
            and replay_b.json()["operation_id"] == body_b["command_id"]
        )
        assert replay_b.json()["correlation_id"] == "corr-tenant-b"

        read_a = client.get(
            f"/platform/v1/operations/{body_a['command_id']}",
            headers={"Authorization": auth_a},
        ).json()
        read_b = client.get(
            f"/platform/v1/operations/{body_b['command_id']}",
            headers={"Authorization": auth_b},
        ).json()
        assert (
            read_a["tenant_id"] == TENANT_A
            and read_a["correlation_id"] == "corr-tenant-a"
        )
        assert (
            read_b["tenant_id"] == TENANT_B
            and read_b["correlation_id"] == "corr-tenant-b"
        )
        if not share_command_id:
            assert (
                client.get(
                    f"/platform/v1/operations/{body_a['command_id']}",
                    headers={"Authorization": auth_b},
                ).status_code
                == 404
            )
            assert (
                client.get(
                    f"/platform/v1/operations/{body_b['command_id']}",
                    headers={"Authorization": auth_a},
                ).status_code
                == 404
            )

        intents = sorted(
            (intent.tenant_id, intent.command_id) for intent in stack.store._outbox
        )
        assert intents == sorted(
            [(TENANT_A, body_a["command_id"]), (TENANT_B, body_b["command_id"])]
        )

        stack.run_bus()
        stack.run_bus()
        assert sorted(stack.dispatched) == sorted(
            [(body_a["command_id"], TENANT_A), (body_b["command_id"], TENANT_B)]
        )
        events_a = asyncio.run(
            stack.store.list_events(TENANT_A, UUID(body_a["command_id"]), limit=100)
        )
        events_b = asyncio.run(
            stack.store.list_events(TENANT_B, UUID(body_b["command_id"]), limit=100)
        )
        assert events_a[0].safe_metadata["principal"]["tenants"] == [TENANT_A]
        assert events_b[0].safe_metadata["principal"]["tenants"] == [TENANT_B]


# ----------------------------------------------------------------------------
# kernel boundary: an unauthorized principal learns nothing from idempotency
# ----------------------------------------------------------------------------
def _principal(tenants: tuple[str, ...]) -> KernelPrincipal:
    return KernelPrincipal(
        subject="user-1",
        client_id="middleware-api",
        tenants=tenants,
        roles=(),
        scopes=("platform.command", "platform.command.read"),
        caller=caller_for_client_id("middleware-api"),
    )


@pytest.mark.xfail(
    strict=True,
    raises=CommandConflict,
    reason=(
        "known defect: CommandKernel.submit (app/platform/kernel.py) runs commands.find_existing "
        "before checking principal.authorized_for(command.tenant_id), so a content-conflicting reuse "
        "of a foreign tenant's identity is a 409 oracle; the HTTP route checks the tenant first "
        "(app/platform/api.py submit_command). Remove this marker with the kernel repair."
    ),
)
def test_kernel_submit_denies_foreign_tenant_before_any_idempotency_lookup(
    stack: Stack,
) -> None:
    """The kernel must not reveal whether tenant A holds a command id or
    idempotency key to a principal that is not authorized for A: an exact or
    a content-conflicting reuse must both be a policy denial, never A's
    operation and never a 409 conflict oracle."""
    with TestClient(stack.app) as client:
        owner = submit_a(client)
    kernel = stack.runtime.platform.kernel
    envelope = CommandEnvelope(**{**owner, "capability": "TEST_SYN_EXECUTE"})
    foreign = _principal((TENANT_B,))
    with pytest.raises(PolicyDenied):
        asyncio.run(kernel.submit(envelope, foreign))
    conflicting = envelope.model_copy(update={"payload": {"probe": "different"}})
    with pytest.raises(PolicyDenied):
        asyncio.run(kernel.submit(conflicting, foreign))
    assert stack.snapshot(TENANT_A, owner["command_id"])["resource_version"] == 1
