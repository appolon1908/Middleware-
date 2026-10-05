"""Execution-time fencing of the V3 command kernel.

Idempotency before the gates, principal re-authorization by the bus, lease
proof before the provider effect, atomic dead-lettering, worker resolution
under a lost or exhausted lease, and reconciler recovery of orphaned
in-flight operations — all on the in-memory ledger the PostgreSQL processes
share code with.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.commands import ADAPTER_COMMAND_DESTINATION, CommandConflict, CommandNotFound
from app.core.config import Settings
from app.core.request_guard import RequestGuard, install_request_guard
from app.platform.adapter import ReadbackStatus
from app.platform.adapters.providers import LegacyBridge
from app.platform.kernel import PolicyDenied
from app.platform.memory import MemoryReconciliationSource
from app.security import AuthorizationError
from app.storage import ActiveLease, LeaseLostError, OutboxRecord, ReconciliationError, StorageError
from app.worker import KnownSafeRetryError, OutboxWorker
from tests.test_platform_kernel import TENANT, Harness, envelope, principal
from tests.test_worker import FakeStore, record


@pytest.fixture
def harness(test_settings: Settings) -> Harness:
    return Harness(test_settings)


# ----------------------------------------------------------------------------
# idempotency before the gates
# ----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_exact_replay_returns_the_original_operation_after_the_gates_close(harness: Harness) -> None:
    command = envelope()
    first = await harness.submit(command)
    assert first.policy is not None and first.safety is not None
    # The global kill switch trips after acceptance: a fresh command is denied,
    # the exact replay still answers with the accepted operation.
    harness.platform.safety.trip()
    replay = await harness.submit(command)
    assert replay.duplicate is True
    assert replay.operation.command_id == command.command_id
    assert replay.policy is None and replay.safety is None
    assert len(harness.intents(command.command_id)) == 1
    with pytest.raises(Exception) as denied:
        await harness.submit(envelope())
    assert "safety denied" in str(denied.value)


@pytest.mark.asyncio
async def test_exact_replay_still_requires_the_original_tenant_and_scope_authority(harness: Harness) -> None:
    command = envelope()
    await harness.submit(command)
    with pytest.raises(PolicyDenied):
        await harness.submit(command, principal(scopes=("platform.command.read",)))
    with pytest.raises(PolicyDenied):
        await harness.submit(command, principal(tenants=("tenant-b",)))
    # Same key, different payload is a conflict before any gate runs.
    with pytest.raises(CommandConflict):
        await harness.submit(command.model_copy(update={"payload": {"probe": False}}))


# ----------------------------------------------------------------------------
# principal re-authorization by the bus
# ----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_submission_persists_the_principal_snapshot_without_secrets(harness: Harness) -> None:
    command = envelope()
    await harness.submit(command)
    snapshot = await harness.commands.principal_snapshot(TENANT, command.command_id)
    assert snapshot == {
        "subject": "user-1",
        "client_id": "middleware-api",
        "tenants": [TENANT],
        "roles": [],
        "scopes": ["platform.command", "platform.command.read"],
        "required_scope": "platform.command",
    }
    intent = harness.intents(command.command_id)[0]
    assert intent.payload["_principal"] == snapshot
    assert "token" not in str(intent.payload).lower()


@pytest.mark.asyncio
async def test_bus_refuses_to_execute_without_principal_lineage(harness: Harness) -> None:
    command = envelope()
    # A submission that bypassed the kernel carries no verified principal.
    await harness.commands.submit(
        command, authenticated_subject="user-1", authenticated_client_id="middleware-api",
        destination=ADAPTER_COMMAND_DESTINATION,
    )
    assert await harness.bus.run_once() is True
    operation = await harness.commands.get(TENANT, command.command_id)
    assert operation.state == "dead_lettered"
    assert harness.test_syn.provider_effects == 0
    dead_letter = await harness.commands.get_dead_letter(TENANT, command.command_id)
    assert "principal_lineage_missing" in dead_letter.terminal_reason
    events = await harness.commands.list_events(TENANT, command.command_id, limit=50)
    assert events[-1].new_state == "dead_lettered"


@pytest.mark.asyncio
async def test_bus_refuses_to_execute_when_the_caller_lost_its_registration(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    command = envelope()
    await harness.submit(command)

    def revoked(client_id: str):
        raise AuthorizationError("control-plane caller is no longer registered")

    monkeypatch.setattr("app.platform.bus.caller_for_client_id", revoked)
    assert await harness.bus.run_once() is True
    operation = await harness.commands.get(TENANT, command.command_id)
    assert operation.state == "dead_lettered"
    assert harness.test_syn.provider_effects == 0
    dead_letter = await harness.commands.get_dead_letter(TENANT, command.command_id)
    assert "client_not_registered" in dead_letter.terminal_reason


@pytest.mark.asyncio
async def test_bus_executes_when_the_persisted_principal_is_still_authorized(harness: Harness) -> None:
    command = envelope()
    await harness.submit(command)
    assert await harness.bus.run_once() is True
    assert (await harness.commands.get(TENANT, command.command_id)).state == "completed"
    assert harness.test_syn.provider_effects == 1
    attempts = await harness.commands.list_attempts(TENANT, command.command_id, limit=10)
    assert [attempt.worker_id for attempt in attempts] == ["memory-bus"]


# ----------------------------------------------------------------------------
# lease proof before the provider call
# ----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_bus_refuses_the_provider_call_when_its_lease_was_lost(harness: Harness) -> None:
    command = envelope()
    await harness.submit(command)
    intent = harness.intents(command.command_id)[0]
    lost = ActiveLease(owner="worker-a", fencing_token=3, lost=True)
    outbox = OutboxRecord(
        id=intent.id, tenant_id=intent.tenant_id, destination=intent.destination, event_type=intent.event_type,
        idempotency_key=intent.idempotency_key, payload=dict(intent.payload), attempt_count=1,
        fencing_token=3, lease_owner="worker-a", lease=lost,
    )
    with pytest.raises(LeaseLostError):
        await harness.dispatch(outbox)
    operation = await harness.commands.get(TENANT, command.command_id)
    assert operation.state == "queued"  # attempt closed as a known-safe failure
    assert harness.test_syn.provider_effects == 0
    attempts = await harness.commands.list_attempts(TENANT, command.command_id, limit=10)
    assert len(attempts) == 1 and attempts[0].state == "failed"
    assert (attempts[0].worker_id, attempts[0].fencing_token) == ("worker-a", 3)


# ----------------------------------------------------------------------------
# atomic dead-lettering
# ----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_dead_letter_is_never_recorded_without_a_terminal_transition(harness: Harness) -> None:
    command = envelope()
    await harness.submit(command)
    record_row = await harness.commands.dead_letter(
        TENANT, command.command_id, actor_id="operator", reason="poison payload",
        reason_code="terminal_failure", error_class="non_retryable", terminal_reason="poison payload", poisoned=True,
    )
    operation = await harness.commands.get(TENANT, command.command_id)
    assert operation.state == "dead_lettered" and record_row.poisoned is True
    # Idempotent: a second call neither fails nor duplicates the record.
    again = await harness.commands.dead_letter(
        TENANT, command.command_id, actor_id="operator", reason="poison payload",
        reason_code="terminal_failure", error_class="non_retryable", terminal_reason="poison payload",
    )
    assert again.dead_letter_id == record_row.dead_letter_id
    with pytest.raises(CommandNotFound):
        await harness.commands.dead_letter(
            TENANT, envelope().command_id, actor_id="operator", reason="x",
            reason_code="terminal_failure", error_class="non_retryable", terminal_reason="x",
        )


@pytest.mark.asyncio
async def test_terminal_attempt_history_is_immutable(harness: Harness) -> None:
    command = envelope()
    harness.test_syn.scripts[str(command.command_id)] = ["reject"]
    await harness.submit(command)
    await harness.bus.run_once()
    operation = await harness.commands.get(TENANT, command.command_id)
    assert operation.state == "failed"
    attempts = await harness.commands.list_attempts(TENANT, command.command_id, limit=10)
    assert [attempt.state for attempt in attempts] == ["failed"]
    frozen = attempts[0].model_copy()
    # A later ledger transition opens a new attempt; it never rewrites the old one.
    await harness.commands.transition(TENANT, command.command_id, new_state="queued", actor_id="operator", reason="retry")
    await harness.commands.transition(TENANT, command.command_id, new_state="dispatching", actor_id="w2", reason="attempt 2")
    attempts = await harness.commands.list_attempts(TENANT, command.command_id, limit=10)
    assert attempts[0] == frozen and attempts[1].attempt_number == 2


# ----------------------------------------------------------------------------
# reconciler: orphaned in-flight operations and queued rows
# ----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_reconciler_recovers_an_operation_whose_worker_died_mid_flight(harness: Harness) -> None:
    command = envelope()
    await harness.submit(command)
    await harness.commands.transition(TENANT, command.command_id, new_state="queued", actor_id="dead", reason="q")
    await harness.commands.transition(TENANT, command.command_id, new_state="dispatching", actor_id="dead", reason="attempt 1")
    harness.test_syn.effects[str(command.command_id)] = 1  # the provider applied it
    intent = harness.intents(command.command_id)[0]
    intent.lease_owner, intent.attempt_count = "dead-worker", 1
    intent.reconciliation_required_at = harness.bus.clock() - 10
    intent.lease_until = harness.bus.clock() - 1  # expired
    source = MemoryReconciliationSource(harness.store, clock=harness.bus.clock)
    harness.reconciler.source = source
    decision = await harness.reconciler.run_once()
    assert decision is not None and decision.final_state == "completed"
    assert (await harness.commands.get(TENANT, command.command_id)).state == "completed"
    assert harness.test_syn.executed == []  # read back, never resent


@pytest.mark.asyncio
async def test_reconciler_returns_a_quarantined_row_whose_ledger_is_queued(harness: Harness) -> None:
    command = envelope()
    await harness.submit(command)
    await harness.commands.transition(TENANT, command.command_id, new_state="queued", actor_id="w", reason="q")
    intent = harness.intents(command.command_id)[0]
    intent.reconciliation_required_at = harness.bus.clock() - 10
    source = MemoryReconciliationSource(harness.store, clock=harness.bus.clock)
    harness.reconciler.source = source
    decision = await harness.reconciler.run_once()
    assert decision is not None and decision.action == "retry"
    assert intent.reconciliation_required_at is None and intent.lease_owner is None


# ----------------------------------------------------------------------------
# worker: lost and exhausted leases
# ----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_worker_dead_letters_a_known_safe_failure_on_the_final_attempt() -> None:
    store = FakeStore(record())
    store.record = OutboxRecord(**{**store.record.__dict__, "attempt_count": 3})

    async def handler(item: OutboxRecord) -> None:
        raise KnownSafeRetryError("provider rejected before any write")

    worker = OutboxWorker(
        store, {"provider": handler}, effect_gate=lambda _: True,
        lease_seconds=60, handler_timeout_seconds=45, max_attempts=3,
    )  # type: ignore[arg-type]
    assert await worker.run_once() is True
    assert store.events[-1] == "resolve:dead_letter"


@pytest.mark.asyncio
async def test_worker_hands_the_handler_its_lease_and_resolves_nothing_after_losing_it() -> None:
    store = FakeStore(record())
    seen: list[OutboxRecord] = []

    async def renew(record_id: int, **kwargs: Any) -> None:
        store.events.append("renew")
        raise StorageError("active dispatch ownership/fencing lost during lease renewal")

    store.renew_active_dispatch = renew  # type: ignore[method-assign]

    async def handler(item: OutboxRecord) -> None:
        seen.append(item)
        assert item.lease is not None and item.lease.live
        await asyncio.sleep(0.25)
        assert item.lease.lost is True

    worker = OutboxWorker(
        store, {"provider": handler}, effect_gate=lambda _: True,
        lease_seconds=0.3, handler_timeout_seconds=0.29, max_attempts=3,
    )  # type: ignore[arg-type]
    assert await worker.run_once() is True
    assert seen and seen[0].lease_owner == worker.worker_id
    assert not any(event.startswith("resolve:") for event in store.events)


@pytest.mark.asyncio
async def test_worker_survives_a_refused_resolution() -> None:
    store = FakeStore(record())

    async def refuse(record_id: int, **kwargs: Any) -> None:
        store.events.append("resolve-refused")
        raise ReconciliationError("outbox record is not awaiting reconciliation")

    store.resolve_reconciliation = refuse  # type: ignore[method-assign]

    async def handler(item: OutboxRecord) -> None:
        return None

    worker = OutboxWorker(
        store, {"provider": handler}, effect_gate=lambda _: True,
        lease_seconds=60, handler_timeout_seconds=45,
    )  # type: ignore[arg-type]
    assert await worker.run_once() is True  # the loop keeps running; the row stays quarantined
    assert store.events[-1] == "resolve-refused"


# ----------------------------------------------------------------------------
# legacy bridge read-back semantics
# ----------------------------------------------------------------------------
class _AcceptedOnlyLegacy:
    async def execute(self, request: Any) -> Any:
        raise AssertionError("not used")

    async def readback(self, request: Any) -> Any:
        class Result:
            status = "accepted"
            provider_operation_id = "prov-1"
            readback_evidence = {"state": "accepted"}

        return Result()


@pytest.mark.asyncio
async def test_legacy_readback_that_only_acknowledges_is_not_a_match(harness: Harness) -> None:
    bridge = LegacyBridge(adapter_id="legacy-x", provider_family="x", connector_ids=("x",), served_capabilities=("CAP",), legacy=_AcceptedOnlyLegacy())
    command = envelope()
    await harness.submit(command)
    operation = await harness.commands.get(TENANT, command.command_id)
    context = harness.dispatch.context(operation, attempt=1, timeout=1.0, payload=command.payload)
    readback = await bridge.readback(operation, context)
    assert readback.status is ReadbackStatus.UNAVAILABLE
    assert readback.safe_error_code == "readback_accepted"


# ----------------------------------------------------------------------------
# request guard: request, causation and environment authority
# ----------------------------------------------------------------------------
def _guarded_app(settings: Settings) -> FastAPI:
    app = FastAPI()
    install_request_guard(app, RequestGuard(settings))

    @app.get("/probe")
    async def probe(request: Request) -> dict[str, Any]:
        return {
            "request_id": request.state.request_id,
            "causation_id": request.state.causation_id,
            "correlation_id": request.state.correlation_id,
        }

    return app


def test_guard_echoes_request_identity_and_refuses_malformed_lineage(test_settings: Settings) -> None:
    with TestClient(_guarded_app(test_settings)) as client:
        echoed = client.get("/probe", headers={"X-Request-ID": "req-1", "X-Causation-ID": "cause-1", "X-Correlation-ID": "corr/1"})
        assert echoed.status_code == 200
        assert echoed.headers["X-Request-ID"] == "req-1"
        assert echoed.headers["X-Correlation-ID"] == "corr/1"
        assert echoed.json() == {"request_id": "req-1", "causation_id": "cause-1", "correlation_id": "corr/1"}

        generated = client.get("/probe")
        assert generated.status_code == 200
        assert generated.headers["X-Request-ID"] == generated.json()["request_id"]
        assert generated.json()["causation_id"] is None

        assert client.get("/probe", headers={"X-Request-ID": "bad id"}).status_code == 400
        assert client.get("/probe", headers={"X-Causation-ID": "x" * 181}).status_code == 400
        # A caller that names another deployment environment is refused.
        assert client.get("/probe", headers={"X-Environment": "production"}).status_code == 400
        assert client.get("/probe", headers={"X-Environment": test_settings.app_env}).status_code == 200
