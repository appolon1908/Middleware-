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
import json
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
from app.platform.kernel import (
    FOREIGN_PRINCIPAL_ACTOR,
    PLATFORM_AUDIT_TENANT,
    DenialAudit,
    MemoryDenialAuditSink,
    PolicyDenied,
)
from app.platform.persistence import PostgresDenialAuditSink
from app.platform.principal import principal_from_claims, tenants_from_claims
from app.security import AuthorizationError, authorize_tenant
from app.platform.memory import MemoryExecutionBus
from app.platform.resilience import ReplayMode
from app.platform.principal import KernelPrincipal, with_synthetic_authority
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
            headers=mutation_headers(attacker, key="resolve-aaaa"),
        )
        assert_non_disclosing(own_tenant, owner, status=404)
        named_victim = client.post(
            f"/platform/v1/reconciliation/{op}/resolve",
            json=body,
            headers=mutation_headers(
                attacker, key="resolve-bbbb", tenant_header=TENANT_A
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
def _principal(
    stack: Stack,
    tenants: tuple[str, ...],
    *,
    scopes: tuple[str, ...] = ("platform.command", "platform.command.read"),
    roles: tuple[str, ...] = (),
    subject: str = "user-1",
) -> KernelPrincipal:
    return KernelPrincipal(
        subject=subject,
        client_id="middleware-api",
        tenants=tenants,
        roles=roles,
        scopes=scopes,
        # the caller authority the HTTP route resolves for this client
        caller=with_synthetic_authority(
            caller_for_client_id("middleware-api"), stack.runtime.settings.app_env
        ),
    )


def _operator(stack: Stack, tenants: tuple[str, ...]) -> KernelPrincipal:
    return _principal(
        stack,
        tenants,
        scopes=(
            "platform.command",
            "platform.command.read",
            "platform.command.replay",
        ),
        roles=OPERATOR_ROLES,
    )


# Every store read or write keyed by tenant that a kernel submission or
# mutation can reach before deciding.
STORE_LOOKUPS = (
    "find_existing",
    "submit",
    "get",
    "mutate_operation",
    "get_dead_letter",
    "create_replay",
    "load_envelope",
)


def record_store_lookups(store: MemoryCommandStore) -> list[str]:
    calls: list[str] = []
    for name in STORE_LOOKUPS:
        original = getattr(store, name)

        def recorder(*args: Any, _name: str = name, _original=original, **kwargs: Any):
            calls.append(_name)
            return _original(*args, **kwargs)

        setattr(store, name, recorder)
    return calls


def test_kernel_submit_denies_foreign_tenant_before_any_idempotency_lookup(
    stack: Stack,
) -> None:
    """The kernel must not reveal whether tenant A holds a command id or
    idempotency key to a principal that is not authorized for A: an exact
    reuse (whose identity digest still differs by authenticated client), a
    content-conflicting reuse and a reuse of the command id under a fresh key
    must all be a policy denial, never A's operation and never a 409
    conflict oracle, and must not touch the store at all."""
    with TestClient(stack.app) as client:
        owner = submit_a(client)
    kernel = stack.runtime.platform.kernel
    before = stack.snapshot(TENANT_A, owner["command_id"])
    denials = stack.runtime.platform.denials
    assert isinstance(denials, MemoryDenialAuditSink)
    denials_before = len(denials.records)
    lookups = record_store_lookups(stack.store)

    envelope = CommandEnvelope(**{**owner, "capability": "TEST_SYN_EXECUTE"})
    # The owner submitted as "user-1"; the foreign principal is a different
    # subject so the audit rows can tell the two apart.
    foreign = _principal(stack, (TENANT_B,), subject=FOREIGN_SUBJECT)
    assert owner["requested_by"] != FOREIGN_SUBJECT
    identical = envelope
    conflicting = envelope.model_copy(update={"payload": {"probe": "different"}})
    reused_command_id = envelope.model_copy(
        update={"idempotency_key": "idem-" + uuid4().hex}
    )
    for attempt in (identical, conflicting, reused_command_id):
        try:
            asyncio.run(kernel.submit(attempt, foreign))
        except CommandConflict as exc:
            pytest.fail(f"foreign tenant received a conflict oracle: {exc}")
        except PolicyDenied as exc:
            assert str(exc) == "policy denied: tenant_not_authorized"
        else:
            pytest.fail("foreign tenant submission was not denied")

    assert lookups == []
    recorded = denials.records[denials_before:]
    victim_rows = [item for item in recorded if item.tenant_id == TENANT_A]
    platform_rows = [item for item in recorded if item.tenant_id == PLATFORM_AUDIT_TENANT]
    assert len(victim_rows) == len(platform_rows) == 3
    assert len(recorded) == 6
    # (a) Q-B4: the row under the victim tenant names the caller only as
    # "foreign principal", with no caller client and no caller correlation id.
    for item in victim_rows:
        assert item.reason_code == "tenant_not_authorized"
        assert item.actor_id == FOREIGN_PRINCIPAL_ACTOR
        assert item.client_id is None
        assert item.correlation_id != envelope.correlation_id
        assert item.correlation_id == item.decision_id
    # (b) the operator-only platform row holds the full identity.
    for item in platform_rows:
        assert item.actor_id == FOREIGN_SUBJECT
        assert item.client_id == foreign.client_id
        assert item.correlation_id == envelope.correlation_id
        code, _, detail = item.reason_code.partition(" ")
        assert code == "tenant_not_authorized"
        assert json.loads(detail) == {
            "target_tenant": TENANT_A,
            "principal_tenants": [TENANT_B],
        }
    assert {item.decision_id for item in victim_rows} == {
        item.decision_id for item in platform_rows
    }
    # (e) nothing the victim tenant can read carries the foreign identity.
    assert_no_foreign_identity(denials.records, TENANT_A, foreign, envelope.correlation_id)
    assert stack.snapshot(TENANT_A, owner["command_id"]) == before
    assert asyncio.run(stack.store.list_operations(TENANT_B, limit=100)) == []


FOREIGN_SUBJECT = "foreign-user-9"


def assert_no_foreign_identity(
    records: list[DenialAudit],
    tenant_id: str,
    foreign: KernelPrincipal,
    correlation_id: str,
) -> None:
    """No denial row under ``tenant_id`` carries the foreign subject, its
    client or the correlation id it supplied."""
    for item in records:
        if item.tenant_id != tenant_id:
            continue
        assert FOREIGN_SUBJECT not in (item.actor_id, item.reason_code)
        assert item.actor_id != foreign.subject
        assert item.correlation_id != correlation_id
        if item.actor_id == FOREIGN_PRINCIPAL_ACTOR:
            assert item.client_id is None


def test_kernel_rejects_foreign_principal_before_mission_scope_evaluation(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(c) a foreign principal is refused on tenant authority before the
    mission-scope check runs, so even a principal that also lacks the mission
    scope records nothing under the victim tenant but the redacted row."""
    import app.identity_missions as identity_missions

    evaluated: list[str] = []

    def no_mission_scope(command_type: str, scopes: tuple[str, ...]) -> bool:
        evaluated.append(command_type)
        return False

    monkeypatch.setattr(identity_missions, "authorize_mission", no_mission_scope)
    kernel = stack.runtime.platform.kernel
    denials = stack.runtime.platform.denials
    assert isinstance(denials, MemoryDenialAuditSink)
    envelope = CommandEnvelope(**command_body(TENANT_A))
    foreign = _principal(stack, (TENANT_B,), subject=FOREIGN_SUBJECT)
    before = len(denials.records)

    with pytest.raises(PolicyDenied, match="^policy denied: tenant_not_authorized$"):
        asyncio.run(kernel.submit(envelope, foreign))

    assert evaluated == []
    recorded = denials.records[before:]
    assert [(item.tenant_id, item.reason_code.split(" ", 1)[0]) for item in recorded] == [
        (PLATFORM_AUDIT_TENANT, "tenant_not_authorized"),
        (TENANT_A, "tenant_not_authorized"),
    ]
    assert_no_foreign_identity(recorded, TENANT_A, foreign, envelope.correlation_id)

    # The same check for an authorized principal is a same-tenant denial and
    # keeps the full identity under its own tenant (unchanged).
    authorized = _principal(stack, (TENANT_A,))
    before = len(denials.records)
    with pytest.raises(PolicyDenied, match="^policy denied: mission_scope_missing$"):
        asyncio.run(kernel.submit(envelope, authorized))
    assert evaluated == [envelope.command_type]
    (own,) = denials.records[before:]
    assert (own.tenant_id, own.actor_id, own.client_id, own.correlation_id, own.reason_code) == (
        TENANT_A,
        "user-1",
        authorized.client_id,
        envelope.correlation_id,
        "mission_scope_missing",
    )


def test_kernel_wildcard_foreign_denial_is_redacted_for_the_target_tenant(
    stack: Stack,
) -> None:
    kernel = stack.runtime.platform.kernel
    denials = stack.runtime.platform.denials
    assert isinstance(denials, MemoryDenialAuditSink)
    envelope = CommandEnvelope(**command_body(TENANT_A))
    wildcard = _principal(stack, ("*",), subject=FOREIGN_SUBJECT)
    before = len(denials.records)

    with pytest.raises(PolicyDenied, match="^policy denied: wildcard_tenant_prohibited$"):
        asyncio.run(kernel.submit(envelope, wildcard))

    victim, = [item for item in denials.records[before:] if item.tenant_id == TENANT_A]
    assert (victim.actor_id, victim.client_id, victim.reason_code) == (
        FOREIGN_PRINCIPAL_ACTOR,
        None,
        "wildcard_tenant_prohibited",
    )
    assert_no_foreign_identity(denials.records, TENANT_A, wildcard, envelope.correlation_id)


def test_platform_audit_tenant_can_never_be_an_authorized_tenant(stack: Stack) -> None:
    """The operator-only pseudo-tenant cannot be selected by any tenant feed:
    even a token that claims it is not authorized for it, by the HTTP tenant
    check or by the kernel principal."""
    caller = caller_for_client_id("middleware-api")
    for claim in (
        {"tenant_id": PLATFORM_AUDIT_TENANT},
        {"tenant_ids": [PLATFORM_AUDIT_TENANT]},
        {"tenant_id": TENANT_B, "tenant_ids": [PLATFORM_AUDIT_TENANT, TENANT_A]},
    ):
        with pytest.raises(AuthorizationError):
            authorize_tenant(dict(claim), PLATFORM_AUDIT_TENANT)
        assert PLATFORM_AUDIT_TENANT not in tenants_from_claims(claim)
        principal = principal_from_claims(
            {"sub": FOREIGN_SUBJECT, "azp": caller.client_id, **claim}, caller
        )
        assert not principal.authorized_for(PLATFORM_AUDIT_TENANT)


def test_durable_victim_row_never_carries_the_foreign_identity(stack: Stack) -> None:
    """(e) what the Postgres sink writes under the victim tenant, i.e. what
    GET /v1/audit/events returns to it (actor_id, reason) and keeps in
    metadata, holds no foreign subject, client or correlation id."""

    class Connection:
        def __init__(self, sink: list[tuple[Any, ...]]) -> None:
            self.sink = sink

        async def execute(self, _sql: str, *args: Any) -> None:
            self.sink.append(args)

    class Acquire:
        def __init__(self, connection: Connection) -> None:
            self.connection = connection

        async def __aenter__(self) -> Connection:
            return self.connection

        async def __aexit__(self, *_exc: Any) -> None:
            return None

    class Pool:
        def __init__(self) -> None:
            self.rows: list[tuple[Any, ...]] = []

        def acquire(self) -> Acquire:
            return Acquire(Connection(self.rows))

    pool = Pool()
    kernel = stack.runtime.platform.kernel
    original = kernel.denials
    kernel.denials = PostgresDenialAuditSink(pool)  # type: ignore[arg-type]
    try:
        envelope = CommandEnvelope(**command_body(TENANT_A))
        foreign = _principal(stack, (TENANT_B,), subject=FOREIGN_SUBJECT)
        with pytest.raises(PolicyDenied):
            asyncio.run(kernel.submit(envelope, foreign))
    finally:
        kernel.denials = original

    by_tenant = {row[0]: row for row in pool.rows}
    assert set(by_tenant) == {TENANT_A, PLATFORM_AUDIT_TENANT}
    tenant_id, _resource_id, action, actor_id, reason, metadata = by_tenant[TENANT_A]
    assert (action, actor_id, reason) == ("policy_deny", FOREIGN_PRINCIPAL_ACTOR, "tenant_not_authorized")
    stored = json.loads(metadata)
    assert stored["client_id"] is None
    assert stored["correlation_id"] != envelope.correlation_id
    victim_text = json.dumps(by_tenant[TENANT_A])
    assert FOREIGN_SUBJECT not in victim_text
    assert envelope.correlation_id not in victim_text
    platform = by_tenant[PLATFORM_AUDIT_TENANT]
    assert platform[3] == FOREIGN_SUBJECT
    assert json.loads(platform[5])["correlation_id"] == envelope.correlation_id


def test_kernel_submit_exact_duplicate_by_authorized_tenant_returns_original_operation(
    stack: Stack,
) -> None:
    kernel = stack.runtime.platform.kernel
    authorized = _principal(stack, (TENANT_A,))
    envelope = CommandEnvelope(**command_body(TENANT_A))
    first = asyncio.run(kernel.submit(envelope, authorized))
    assert first.operation.duplicate is False
    before = stack.snapshot(TENANT_A, str(envelope.command_id))

    result = asyncio.run(kernel.submit(envelope, authorized))

    assert result.operation.command_id == envelope.command_id
    assert result.operation.tenant_id == TENANT_A
    assert result.operation.correlation_id == envelope.correlation_id
    assert result.operation.duplicate is True
    assert result.policy is None and result.safety is None
    assert stack.snapshot(TENANT_A, str(envelope.command_id)) == before


def _kernel_cancel(kernel, op: UUID, version: int, principal: KernelPrincipal):
    return kernel.cancel(
        TENANT_A,
        op,
        principal=principal,
        idempotency_key="attack-kernel-cancel",
        expected_version=version,
        reason="attack",
    )


def _kernel_retry(kernel, op: UUID, version: int, principal: KernelPrincipal):
    return kernel.retry(
        TENANT_A,
        op,
        principal=principal,
        idempotency_key="attack-kernel-retry",
        expected_version=version,
        reason="attack",
    )


def _kernel_reprocess(kernel, op: UUID, version: int, principal: KernelPrincipal):
    return kernel.replay(
        TENANT_A,
        op,
        principal=principal,
        mode=ReplayMode.REPROCESS,
        idempotency_key="attack-kernel-reprocess",
        expected_version=version,
        reason="attack",
    )


def _kernel_reexecute(kernel, op: UUID, version: int, principal: KernelPrincipal):
    return kernel.replay(
        TENANT_A,
        op,
        principal=principal,
        mode=ReplayMode.REEXECUTE,
        idempotency_key="attack-kernel-reexecute",
        expected_version=version,
        reason="attack",
        new_idempotency_key="attacker-new-key-0003",
    )


# (name, call, fixture behaviour) — each arrangement leaves A's operation in a
# state where the call would succeed for an authorized operator.
KERNEL_MUTATIONS = (
    ("cancel", _kernel_cancel, "success", False),
    ("retry", _kernel_retry, "reject", True),
    ("replay_reprocess", _kernel_reprocess, "unknown", True),
    ("replay_reexecute", _kernel_reexecute, "reject", True),
)


@pytest.mark.parametrize(
    "name, call, behaviour, execute",
    KERNEL_MUTATIONS,
    ids=[item[0] for item in KERNEL_MUTATIONS],
)
def test_kernel_mutations_deny_foreign_tenant_before_any_lookup(
    stack: Stack, name: str, call, behaviour: str, execute: bool
) -> None:
    with TestClient(stack.app) as client:
        owner = submit_a(client, payload={"fixture": behaviour, "probe": SECRET_MARKER})
    op = UUID(owner["command_id"])
    if execute:
        stack.run_bus()
    if name == "replay_reexecute":
        asyncio.run(
            stack.store.record_dead_letter(
                TENANT_A,
                op,
                actor_id="test",
                reason_code="fixture_rejected",
                error_class="NON_RETRYABLE",
                terminal_reason="arranged",
            )
        )
    kernel = stack.runtime.platform.kernel
    before = stack.snapshot(TENANT_A, owner["command_id"])
    lookups = record_store_lookups(stack.store)

    with pytest.raises(PolicyDenied, match="tenant_not_authorized"):
        asyncio.run(
            call(kernel, op, before["resource_version"], _operator(stack, (TENANT_B,)))
        )
    with pytest.raises(PolicyDenied, match="wildcard_tenant_prohibited"):
        asyncio.run(
            call(kernel, op, before["resource_version"], _operator(stack, ("*",)))
        )

    assert lookups == []
    assert stack.snapshot(TENANT_A, owner["command_id"]) == before


def test_kernel_reexecute_by_authorized_operator_still_submits(stack: Stack) -> None:
    with TestClient(stack.app) as client:
        owner = submit_a(client, payload={"fixture": "reject", "probe": SECRET_MARKER})
    op = UUID(owner["command_id"])
    stack.run_bus()
    asyncio.run(
        stack.store.record_dead_letter(
            TENANT_A,
            op,
            actor_id="test",
            reason_code="fixture_rejected",
            error_class="NON_RETRYABLE",
            terminal_reason="arranged",
        )
    )
    kernel = stack.runtime.platform.kernel
    version = stack.snapshot(TENANT_A, owner["command_id"])["resource_version"]

    replayed = asyncio.run(
        kernel.replay(
            TENANT_A,
            op,
            principal=_operator(stack, (TENANT_A,)),
            mode=ReplayMode.REEXECUTE,
            idempotency_key="operator-reexecute-0001",
            expected_version=version,
            reason="operator",
            new_idempotency_key="operator-new-key-0001",
        )
    )

    assert replayed.command_id != op
    assert replayed.tenant_id == TENANT_A
    assert replayed.duplicate is False
    events = asyncio.run(
        stack.store.list_events(TENANT_A, replayed.command_id, limit=5)
    )
    assert events[0].safe_metadata["replay_mode"] == "REEXECUTE"
    assert events[0].safe_metadata["replay_of"] == str(op)
