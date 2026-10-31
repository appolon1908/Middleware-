"""V3 command kernel on disposable PostgreSQL: the same kernel, ExecutionBus
(the real OutboxWorker over middleware_outbox) and reconciler as production,
against the runtime SQL schema — idempotency concurrency proof, execution to
completion through a MATCHED readback, quarantine + reconciliation, lease
expiry recovery, two workers one effect, fencing, cancel, and the denial
audit in middleware_control_audit.

RUNTIME_INTEGRATION_TESTS=1 with a disposable DATABASE_URL (see conftest).
"""

from __future__ import annotations

import asyncio
import contextvars
import os
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio

from app.commands import ADAPTER_COMMAND_DESTINATION, CommandConflict, CommandEnvelope, CommandService, PostgresCommandStore
from app.control_plane_auth import ControlPlaneCaller
from app.core.config import Settings
from app.platform.adapter import ReadbackStatus
from app.platform.adapters.fixtures import FixtureAdapter, development_fixtures
from app.platform.bus import BusSettings
from app.platform.kernel import SafetyDenied
from app.platform.principal import KernelPrincipal
from app.platform.runtime import build_platform_runtime, command_policies
from app.storage import PostgresOutboxStore
from app.worker import OutboxWorker

DATABASE_URL = os.getenv("DATABASE_URL", "")
RUN = os.getenv("RUNTIME_INTEGRATION_TESTS") == "1"
pytestmark = pytest.mark.skipif(not RUN, reason="set RUNTIME_INTEGRATION_TESTS=1 against disposable PostgreSQL/Redis")
TENANT = "TEST_SYN"
TABLES = (
    "middleware_command_replays",
    "middleware_command_dead_letters",
    "middleware_outbox_attempt_events",
    "middleware_control_mutations",
    "middleware_control_audit",
    "middleware_operation_mutations",
    "middleware_event_ledger",
    "middleware_reconciliation_audit",
    "middleware_outbox",
    "middleware_inbox",
    "middleware_schema_migrations",
    "middleware_command_audit",
    "middleware_command_attempts",
    "middleware_commands",
)


@pytest_asyncio.fixture
async def pool() -> asyncpg.Pool:
    assert DATABASE_URL, "DATABASE_URL is required"
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=8)
    migrations = [path.read_text(encoding="utf-8") for path in sorted(Path("migrations").glob("[0-9][0-9][0-9][0-9]_*.sql"))]
    async with pool.acquire() as conn:
        for table in TABLES:
            await conn.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
        for migration in migrations:
            await conn.execute(migration)
    try:
        yield pool
    finally:
        await pool.close()


def _settings(monkeypatch) -> Settings:
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("ALLOW_IN_MEMORY_STORAGE", "false")
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    return Settings.from_env()


def _principal() -> KernelPrincipal:
    caller = ControlPlaneCaller(client_id="middleware-api", command_scope="platform.command", status_scope="platform.command.read", allowed_command_prefixes=("test.syn.",), allowed_targets=frozenset({"test-syn"}), connector_commands_allowed=True, compatibility_only=False)
    return KernelPrincipal(subject="user-1", client_id="middleware-api", tenants=(TENANT,), roles=(), scopes=("platform.command", "platform.command.read"), caller=caller)


def _envelope(**updates: Any) -> CommandEnvelope:
    value: dict[str, Any] = {
        "command_id": str(uuid4()),
        "command_type": "test.syn.execute.v1",
        "command_version": "1.0",
        "target": "test-syn",
        "tenant_id": TENANT,
        "requested_by": "user-1",
        "correlation_id": "corr-" + uuid4().hex[:12],
        "idempotency_key": "idem-" + uuid4().hex,
        "capability": "TEST_SYN_EXECUTE",
        "payload": {"probe": True},
    }
    value.update(updates)
    return CommandEnvelope.model_validate(value)


class Stack:
    def __init__(self, settings: Settings, pool: asyncpg.Pool, *, worker_id: str = "worker-a", bus_settings: BusSettings | None = None, adapters: tuple[object, ...] | None = None) -> None:
        self.settings = settings
        self.pool = pool
        self.store = PostgresCommandStore(pool, owns_pool=False)
        self.commands = CommandService(store=self.store, policies=command_policies(settings))
        self.platform = build_platform_runtime(settings, commands=self.commands, http=None, pool=pool, service_id="middleware-integration-api", adapters=adapters if adapters is not None else development_fixtures(), bus_settings=bus_settings)
        assert self.platform.registry_error is None, self.platform.registry_error
        self.platform.dispatch.worker_id = worker_id
        self.outbox = PostgresOutboxStore(pool)
        self.worker = OutboxWorker(
            self.outbox,
            {ADAPTER_COMMAND_DESTINATION: self.platform.dispatch},
            effect_gate=lambda record: record.destination == ADAPTER_COMMAND_DESTINATION,
            poll_seconds=0.01,
            lease_seconds=60.0,
            handler_timeout_seconds=45.0,
            max_attempts=3,
        )
        self.worker.worker_id = worker_id

    @property
    def test_syn(self) -> FixtureAdapter:
        return self.platform.registry.adapter("test-syn")  # type: ignore[return-value]

    async def submit(self, command: CommandEnvelope):
        return await self.platform.kernel.submit(command, _principal())

    async def outbox_rows(self, command_id: UUID) -> list[asyncpg.Record]:
        async with self.pool.acquire() as conn:
            return await conn.fetch("SELECT * FROM middleware_outbox WHERE tenant_id=$1 AND command_id=$2 ORDER BY id", TENANT, str(command_id))


@pytest.mark.asyncio
async def test_hundred_concurrent_identical_submissions(pool: asyncpg.Pool, monkeypatch) -> None:
    stack = Stack(_settings(monkeypatch), pool)
    command = _envelope()
    results = await asyncio.gather(*(stack.submit(command) for _ in range(100)))
    assert {item.operation.command_id for item in results} == {command.command_id}
    assert sum(1 for item in results if not item.duplicate) == 1
    assert sum(1 for item in results if item.duplicate) == 99
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM middleware_commands WHERE tenant_id=$1", TENANT) == 1
        assert await conn.fetchval("SELECT count(*) FROM middleware_outbox WHERE tenant_id=$1 AND destination=$2", TENANT, ADAPTER_COMMAND_DESTINATION) == 1
        metadata = await conn.fetchval("SELECT metadata FROM middleware_command_audit WHERE tenant_id=$1 AND command_id=$2 ORDER BY id LIMIT 1", TENANT, str(command.command_id))
    assert '"policy_allow": true' in metadata.replace('":', '": ') or "policy_allow" in metadata
    conflicting = await asyncio.gather(*(stack.submit(command.model_copy(update={"payload": {"probe": "changed"}})) for _ in range(10)), return_exceptions=True)
    assert all(isinstance(item, CommandConflict) for item in conflicting)
    # execute exactly once
    assert await stack.worker.run_once() is True
    assert stack.test_syn.provider_effects == 1
    operation = await stack.commands.get(TENANT, command.command_id)
    assert operation.state == "completed" and operation.readback_evidence_sha256
    rows = await stack.outbox_rows(command.command_id)
    assert len(rows) == 1 and rows[0]["completed_at"] is not None and rows[0]["reconciliation_required_at"] is None
    events = await stack.commands.list_events(TENANT, command.command_id, limit=20)
    assert [event.new_state for event in events] == ["persisted", "queued", "dispatching", "accepted", "readback_pending", "completed"]
    assert [event.event_id for event in events] == sorted(event.event_id for event in events)


@pytest.mark.asyncio
async def test_unknown_outcome_is_quarantined_then_reconciled(pool: asyncpg.Pool, monkeypatch) -> None:
    stack = Stack(_settings(monkeypatch), pool)
    command = _envelope(payload={"fixture": "unknown"})
    await stack.submit(command)
    assert await stack.worker.run_once() is True
    operation = await stack.commands.get(TENANT, command.command_id)
    assert operation.state == "reconciliation_required"
    rows = await stack.outbox_rows(command.command_id)
    assert rows[0]["reconciliation_required_at"] is not None and rows[0]["completed_at"] is None
    assert await stack.worker.run_once() is False  # quarantined rows are never re-claimed by the bus
    # the reconciler must wait for the worker lease to expire
    async with pool.acquire() as conn:
        await conn.execute("UPDATE middleware_outbox SET lease_until=now() - interval '1 second' WHERE command_id=$1", str(command.command_id))
    reconciler = stack.platform.reconciler
    assert reconciler is not None
    decision = await reconciler.run_once()
    assert decision is not None and decision.action == "complete"
    operation = await stack.commands.get(TENANT, command.command_id)
    assert operation.state == "completed" and operation.readback_evidence["reconciled"] is True
    assert stack.test_syn.provider_effects == 1
    async with pool.acquire() as conn:
        audit = await conn.fetch("SELECT action FROM middleware_reconciliation_audit ORDER BY id")
        claims = await conn.fetchval("SELECT count(*) FROM middleware_control_audit WHERE resource_kind='outbox_reconciliation'")
    assert [row["action"] for row in audit] == ["complete"] and claims == 1


@pytest.mark.asyncio
async def test_lease_expiry_recovers_by_readback_without_resend(pool: asyncpg.Pool, monkeypatch) -> None:
    stack = Stack(_settings(monkeypatch), pool)
    command = _envelope()
    await stack.submit(command)
    # Simulate a worker that opened the attempt, reached the provider and died.
    await stack.commands.transition(TENANT, command.command_id, new_state="queued", actor_id="dead", reason="q")
    await stack.commands.transition(TENANT, command.command_id, new_state="dispatching", actor_id="dead", reason="attempt 1")
    stack.test_syn.effects[str(command.command_id)] = 1
    async with pool.acquire() as conn:
        await conn.execute("UPDATE middleware_outbox SET lease_owner='dead', lease_until=now() - interval '1 second', attempt_count=1 WHERE command_id=$1", str(command.command_id))
    assert await stack.worker.run_once() is True
    operation = await stack.commands.get(TENANT, command.command_id)
    assert operation.state == "completed"
    assert stack.test_syn.executed == []  # readback discovered the effect; nothing re-sent
    assert stack.test_syn.effects[str(command.command_id)] == 1


@pytest.mark.asyncio
async def test_two_workers_one_command_one_effect(pool: asyncpg.Pool, monkeypatch) -> None:
    settings = _settings(monkeypatch)
    first = Stack(settings, pool, worker_id="worker-a")
    second = Stack(settings, pool, worker_id="worker-b")
    # both stacks share the ledger; the fixture adapter instances differ, so count effects across both
    command = _envelope()
    await first.submit(command)
    claimed = await asyncio.gather(first.worker.run_once(), second.worker.run_once())
    assert sorted(claimed) == [False, True]
    assert first.test_syn.provider_effects + second.test_syn.provider_effects == 1
    assert (await first.commands.get(TENANT, command.command_id)).state == "completed"


@pytest.mark.asyncio
async def test_stale_attempt_fencing_and_cancel_semantics(pool: asyncpg.Pool, monkeypatch) -> None:
    stack = Stack(_settings(monkeypatch), pool)
    fresh = _envelope()
    await stack.submit(fresh)
    cancelled = await stack.platform.kernel.cancel(TENANT, fresh.command_id, principal=_principal(), idempotency_key="cancel-0000001", expected_version=1, reason="operator", mutation_correlation_id="cancel-request-corr")
    assert cancelled.state == "cancelled"
    events = await stack.commands.list_events(TENANT, fresh.command_id, limit=20)
    assert events[-1].safe_metadata["mutation_correlation_id"] == "cancel-request-corr"
    assert cancelled.correlation_id != "cancel-request-corr"
    rows = await stack.outbox_rows(fresh.command_id)
    assert rows[0]["cancelled_at"] is not None
    assert await stack.worker.run_once() is False  # a cancelled intent is never claimed

    command = _envelope()
    await stack.submit(command)
    await stack.commands.transition(TENANT, command.command_id, new_state="queued", actor_id="w", reason="q")
    await stack.commands.transition(TENANT, command.command_id, new_state="dispatching", actor_id="w", reason="attempt 1")
    await stack.commands.transition(TENANT, command.command_id, new_state="failed", actor_id="w", reason="x", expected_attempt=1)
    await stack.commands.transition(TENANT, command.command_id, new_state="queued", actor_id="w", reason="retry")
    await stack.commands.transition(TENANT, command.command_id, new_state="dispatching", actor_id="w", reason="attempt 2")
    with pytest.raises(CommandConflict, match="stale attempt fencing"):
        await stack.commands.transition(TENANT, command.command_id, new_state="accepted", actor_id="stale", reason="late", expected_attempt=1)
    assert await stack.commands.latest_attempt(TENANT, command.command_id) == 2
    # A dispatching operation whose worker vanished is recovered by readback, never re-sent.
    stack.test_syn.effects[str(command.command_id)] = 1
    assert await stack.worker.run_once() is True
    assert (await stack.commands.get(TENANT, command.command_id)).state == "completed"
    assert stack.test_syn.executed == []


@pytest.mark.asyncio
async def test_reconciler_requeues_not_found_and_dead_letters_after_budget(pool: asyncpg.Pool, monkeypatch) -> None:
    stack = Stack(_settings(monkeypatch), pool)
    lost = _envelope()
    stack.test_syn.scripts[str(lost.command_id)] = ["crash", "success"]
    await stack.submit(lost)
    await stack.worker.run_once()
    assert (await stack.commands.get(TENANT, lost.command_id)).state == "reconciliation_required"
    stack.test_syn.effects.pop(str(lost.command_id), None)  # provider has no trace: the effect never happened
    async with pool.acquire() as conn:
        await conn.execute("UPDATE middleware_outbox SET lease_until=now() - interval '1 second' WHERE command_id=$1", str(lost.command_id))
    decision = await stack.platform.reconciler.run_once()
    assert decision.action == "retry" and decision.final_state == "queued"
    assert await stack.worker.run_once() is True
    assert (await stack.commands.get(TENANT, lost.command_id)).state == "completed"

    stuck = _envelope(payload={"fixture": "unknown"})
    await stack.submit(stuck)
    await stack.worker.run_once()
    stack.test_syn.reconcile_as[str(stuck.command_id)] = ReadbackStatus.MISMATCH
    stack.platform.reconciler.budget = 2
    for _ in range(2):
        async with pool.acquire() as conn:
            await conn.execute("UPDATE middleware_outbox SET lease_until=now() - interval '1 second' WHERE command_id=$1", str(stuck.command_id))
        decision = await stack.platform.reconciler.run_once()
    assert decision.action == "dead_letter"
    assert (await stack.commands.get(TENANT, stuck.command_id)).state == "dead_lettered"
    rows = await stack.outbox_rows(stuck.command_id)
    assert rows[0]["dead_lettered_at"] is not None


@pytest.mark.asyncio
async def test_denials_are_audited_and_backlog_is_bounded(pool: asyncpg.Pool, monkeypatch) -> None:
    stack = Stack(_settings(monkeypatch), pool)
    with pytest.raises(SafetyDenied):
        await stack.platform.kernel.submit(_envelope(command_type="crm.contact.create.v1", target="odoo-19", capability="ODOO_WRITE"), KernelPrincipal(subject="user-1", client_id="odoo-integration", tenants=(TENANT,), roles=(), scopes=("platform.command",), caller=ControlPlaneCaller(client_id="odoo-integration", command_scope="platform.command", status_scope="platform.command.read", allowed_command_prefixes=("crm.",), allowed_targets=frozenset({"odoo-19"}), connector_commands_allowed=True, compatibility_only=False)))
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT action, new_state, reason, metadata FROM middleware_control_audit WHERE resource_kind='command_submission'")
    assert row["action"] == "safety_deny" and row["new_state"] == "denied"
    assert "capability" in row["metadata"] and "secret" not in row["metadata"].lower()
    tenant_active, global_active = await stack.commands.backlog(TENANT)
    assert tenant_active == 0 and global_active is None
    await stack.submit(_envelope())
    assert await stack.commands.backlog(TENANT) == (1, None)


@pytest.mark.asyncio
async def test_chaos_b_rollback_leaves_no_orphan_command_or_intent(pool: asyncpg.Pool, monkeypatch) -> None:
    """Chaos B: a failure after the command insert but before the outbox intent
    rolls the whole acceptance back (no command, no audit, no intent)."""
    stack = Stack(_settings(monkeypatch), pool)
    command = _envelope()
    original = stack.store.submit_on_connection

    class CrashingConnection:
        """Proxies the pooled connection; the outbox insert raises after the
        command row and its audit are already written inside the transaction."""

        def __init__(self, conn) -> None:
            self._conn = conn

        def __getattr__(self, name):
            return getattr(self._conn, name)

        async def execute(self, query, *args, **kw):
            if "INSERT INTO middleware_outbox" in query:
                raise RuntimeError("simulated crash before the outbox intent")
            return await self._conn.execute(query, *args, **kw)

    async def crash_after_command_insert(conn, envelope, **kwargs):
        return await original(CrashingConnection(conn), envelope, **kwargs)

    stack.store.submit_on_connection = crash_after_command_insert  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="simulated crash"):
        await stack.submit(command)
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM middleware_commands WHERE command_id=$1", str(command.command_id)) == 0
        assert await conn.fetchval("SELECT count(*) FROM middleware_command_audit WHERE command_id=$1", str(command.command_id)) == 0
        assert await conn.fetchval("SELECT count(*) FROM middleware_outbox WHERE command_id=$1", str(command.command_id)) == 0
    stack.store.submit_on_connection = original  # type: ignore[method-assign]
    accepted = await stack.submit(command)  # the client retry is a clean first acceptance
    assert accepted.duplicate is False


@pytest.mark.asyncio
async def test_chaos_j_restart_keeps_operation_idempotency_and_timeline(pool: asyncpg.Pool, monkeypatch) -> None:
    """Chaos J: a fresh process (new container, new adapters, new worker) over the
    same database continues from the durable state; the retry is a replay."""
    settings = _settings(monkeypatch)
    before = Stack(settings, pool, worker_id="worker-before")
    command = _envelope()
    accepted = await before.submit(command)
    assert accepted.duplicate is False
    await before.commands.transition(TENANT, command.command_id, new_state="queued", actor_id="w", reason="q")
    # "restart": everything in memory is gone
    after = Stack(settings, pool, worker_id="worker-after")
    replay = await after.submit(command)
    assert replay.duplicate is True and replay.operation.state == "queued"
    assert await after.worker.run_once() is True
    operation = await after.commands.get(TENANT, command.command_id)
    assert operation.state == "completed"
    events = await after.commands.list_events(TENANT, command.command_id, limit=20)
    assert [event.new_state for event in events] == ["persisted", "queued", "dispatching", "accepted", "readback_pending", "completed"]
    assert after.test_syn.provider_effects == 1 and before.test_syn.provider_effects == 0


# ----------------------------------------------------------------------------
# Section 5: cancellation and retry races on PostgreSQL (the real OutboxWorker)
# ----------------------------------------------------------------------------
STRANDED_COMMANDS = """
SELECT c.command_id FROM middleware_commands c
WHERE c.tenant_id=$1
  AND c.state IN ('persisted','queued','dispatching','accepted','readback_pending','reconciliation_required')
  AND NOT EXISTS (
    SELECT 1 FROM middleware_outbox o
    WHERE o.tenant_id=c.tenant_id AND o.command_id=c.command_id
      AND o.completed_at IS NULL AND o.dead_lettered_at IS NULL AND o.cancelled_at IS NULL)
"""


def _operator() -> KernelPrincipal:
    base = _principal()
    return KernelPrincipal(
        subject="operator-1", client_id=base.client_id, tenants=base.tenants, roles=("platform-operator",),
        scopes=("platform.command", "platform.command.read", "platform.command.replay"), caller=base.caller,
    )


def _racing_stack(settings: Settings, pool: asyncpg.Pool, *, safe: bool = True, max_attempts: int | None = None) -> Stack:
    from tests.test_cancel_retry_races import racing

    bus_settings = BusSettings(max_attempts=max_attempts) if max_attempts is not None else BusSettings(max_attempts=3)
    return Stack(settings, pool, adapters=(racing(safe=safe), *development_fixtures()[1:]), bus_settings=bus_settings)


async def _cancel(stack: Stack, command_id: UUID, key: str) -> Any:
    current = await stack.commands.get(TENANT, command_id)
    return await stack.platform.kernel.cancel(
        TENANT, command_id, principal=_principal(), idempotency_key=key,
        expected_version=current.resource_version, reason="operator cancel",
    )


async def _stranded(pool: asyncpg.Pool) -> list[str]:
    async with pool.acquire() as conn:
        return [row["command_id"] for row in await conn.fetch(STRANDED_COMMANDS, TENANT)]


async def _expire(pool: asyncpg.Pool, command_id: UUID) -> None:
    async with pool.acquire() as conn:
        await conn.execute("UPDATE middleware_outbox SET lease_until=now() - interval '1 second' WHERE command_id=$1", str(command_id))


@pytest.mark.asyncio
async def test_cancel_before_dispatch_while_worker_holds_lease(pool: asyncpg.Pool, monkeypatch) -> None:
    stack = _racing_stack(_settings(monkeypatch), pool)
    adapter = stack.test_syn
    command = _envelope()
    await stack.submit(command)
    seen: dict[str, Any] = {}

    async def cancel_while_leased() -> None:
        seen["cancel"] = await _cancel(stack, command.command_id, "cancel-pg-lease")

    adapter.before_readiness = cancel_while_leased  # type: ignore[attr-defined]
    assert await stack.worker.run_once() is True
    assert seen["cancel"].state == "cancelled" and seen["cancel"].cancelled_at is not None
    assert adapter.executed == [] and adapter.provider_effects == 0
    assert stack.platform.dispatch.last_outcome is not None and stack.platform.dispatch.last_outcome.final_state == "cancelled"
    rows = await stack.outbox_rows(command.command_id)
    assert rows[0]["completed_at"] is not None and rows[0]["reconciliation_required_at"] is None
    assert await stack.commands.latest_attempt(TENANT, command.command_id) == 0
    assert await _stranded(pool) == []


RACE_STEP_TIMEOUT = 10.0
RACE_ROUND_TIMEOUT = 30.0
# Set inside the cancelling task so G2 only ever holds the worker's dispatch
# (the outbox worker runs its handler in a child task of its own).
_IN_CANCEL: contextvars.ContextVar[bool] = contextvars.ContextVar("in_cancel", default=False)


@pytest.mark.asyncio
async def test_concurrent_cancel_never_executes_after_a_committed_cancel(pool: asyncpg.Pool, monkeypatch) -> None:
    """20 rounds of a cancel against a worker holding the lease, with two gates:
    G1 in readiness (before queued -> dispatching) and G2 right after the
    attempt opened (the bus's ``latest_attempt`` read, before the provider
    call). Rounds rotate between cancelling at G1, cancelling at G2 and a free
    race that releases G1 concurrently with the cancel, so both branches are
    exercised deterministically and tallied from the observed outcome."""
    stack = _racing_stack(_settings(monkeypatch), pool)
    adapter = stack.test_syn
    original_latest_attempt = stack.store.latest_attempt
    tally = {"cancel_before_dispatch": 0, "cancel_after_attempt_open": 0}
    for index in range(20):
        mode = ("at_readiness", "after_open", "race")[index % 3]
        command = _envelope()
        await stack.submit(command)
        at_g1, release_g1 = asyncio.Event(), asyncio.Event()
        at_g2, cancel_done = asyncio.Event(), asyncio.Event()

        async def gate_readiness() -> None:
            at_g1.set()
            await asyncio.wait_for(release_g1.wait(), RACE_STEP_TIMEOUT)

        async def latest_attempt_gate(tenant_id: str, cid: UUID, command_id: UUID = command.command_id) -> int:
            value = await original_latest_attempt(tenant_id, cid)
            if cid == command_id and not _IN_CANCEL.get() and not at_g2.is_set():
                # The attempt is open (dispatching committed); hold before the
                # pre-send re-read until the racing cancel has returned.
                at_g2.set()
                await asyncio.wait_for(cancel_done.wait(), RACE_STEP_TIMEOUT)
            return value

        async def cancel_round(command_id: UUID = command.command_id, key: str = f"cancel-race-{index:04d}", mode: str = mode) -> Any:
            _IN_CANCEL.set(True)
            try:
                await asyncio.wait_for(at_g1.wait(), RACE_STEP_TIMEOUT)
                if mode == "at_readiness":
                    result = await _cancel(stack, command_id, key)
                    release_g1.set()
                elif mode == "after_open":
                    release_g1.set()
                    await asyncio.wait_for(at_g2.wait(), RACE_STEP_TIMEOUT)
                    result = await _cancel(stack, command_id, key)
                else:
                    release_g1.set()
                    result = await _cancel(stack, command_id, key)
                return result
            finally:
                release_g1.set()
                cancel_done.set()

        adapter.before_readiness = gate_readiness  # type: ignore[attr-defined]
        monkeypatch.setattr(stack.store, "latest_attempt", latest_attempt_gate)
        try:
            claimed, cancelled = await asyncio.wait_for(asyncio.gather(stack.worker.run_once(), cancel_round()), RACE_ROUND_TIMEOUT)
        finally:
            monkeypatch.setattr(stack.store, "latest_attempt", original_latest_attempt)
        assert claimed is True
        executed = [item for item in adapter.executed if item[0] == str(command.command_id)]
        effects = adapter.effects.get(str(command.command_id), 0)
        if cancelled.state == "cancelled":
            tally["cancel_before_dispatch"] += 1
            assert cancelled.cancelled_at is not None
            assert executed == [] and effects == 0
            assert await stack.commands.latest_attempt(TENANT, command.command_id) == 0
        else:
            tally["cancel_after_attempt_open"] += 1
            assert cancelled.state == "reconciliation_required" and cancelled.cancelled_at is None
            assert at_g2.is_set()
            assert len(executed) <= 1 and effects <= 1
        if mode == "at_readiness":
            assert cancelled.state == "cancelled"
        if mode == "after_open":
            # Cancelled after the attempt opened, before the provider call: the
            # pre-send re-read stops the send and records it as never sent.
            assert cancelled.state == "reconciliation_required" and executed == [] and effects == 0
            final = await stack.commands.get(TENANT, command.command_id)
            assert final.state == "dead_lettered"
        final = await stack.commands.get(TENANT, command.command_id)
        if final.state == "dead_lettered":
            record = await stack.commands.get_dead_letter(TENANT, command.command_id)
            assert record.reason_code == "cancel_requested_provider_missing"
            if record.error_class == "cancelled_before_send":
                assert executed == [] and effects == 0
    assert tally["cancel_before_dispatch"] > 0, tally
    assert tally["cancel_after_attempt_open"] > 0, tally
    assert sum(tally.values()) == 20
    assert await _stranded(pool) == []


@pytest.mark.asyncio
async def test_cancel_ignores_live_and_expired_leases(pool: asyncpg.Pool, monkeypatch) -> None:
    stack = _racing_stack(_settings(monkeypatch), pool)
    expired, live = _envelope(), _envelope()
    await stack.submit(expired)
    await stack.submit(live)
    async with pool.acquire() as conn:
        await conn.execute("UPDATE middleware_outbox SET lease_owner='dead', lease_until=now() - interval '1 second' WHERE command_id=$1", str(expired.command_id))
        await conn.execute("UPDATE middleware_outbox SET lease_owner='busy', lease_until=now() + interval '60 seconds' WHERE command_id=$1", str(live.command_id))
    assert (await _cancel(stack, expired.command_id, "cancel-pg-expired")).state == "cancelled"
    assert (await _cancel(stack, live.command_id, "cancel-pg-live")).state == "cancelled"
    # The leased row is closed by its owner, never marked cancelled under it.
    assert (await stack.outbox_rows(live.command_id))[0]["cancelled_at"] is None


@pytest.mark.asyncio
async def test_cancel_after_transmission_completes_or_dead_letters(pool: asyncpg.Pool, monkeypatch) -> None:
    stack = _racing_stack(_settings(monkeypatch), pool)
    adapter = stack.test_syn
    reconciler = stack.platform.reconciler
    assert reconciler is not None

    sent = _envelope()
    await stack.submit(sent)

    async def cancel_after_send() -> None:
        assert (await _cancel(stack, sent.command_id, "cancel-pg-sent")).state == "reconciliation_required"

    adapter.after_send = cancel_after_send  # type: ignore[attr-defined]
    await stack.worker.run_once()
    parked = await stack.commands.get(TENANT, sent.command_id)
    assert parked.state == "reconciliation_required" and parked.provider_operation_id == f"test-syn:{sent.command_id}:1"
    await _expire(pool, sent.command_id)
    decision = await reconciler.run_once()
    assert decision is not None and decision.final_state == "completed"
    assert adapter.effects[str(sent.command_id)] == 1

    lost = _envelope(payload={"fixture": "unknown"})
    await stack.submit(lost)

    async def cancel_and_lose() -> None:
        await _cancel(stack, lost.command_id, "cancel-pg-lost")
        adapter.effects.pop(str(lost.command_id), None)

    adapter.after_send = cancel_and_lose  # type: ignore[attr-defined]
    await stack.worker.run_once()
    await _expire(pool, lost.command_id)
    decision = await reconciler.run_once()
    assert decision is not None and decision.final_state == "dead_lettered"
    record = await stack.commands.get_dead_letter(TENANT, lost.command_id)
    assert record.reason_code == "cancel_requested_provider_missing" and record.error_class == "not_found"
    assert await stack.worker.run_once() is False
    assert len([item for item in adapter.executed if item[0] == str(lost.command_id)]) == 1
    assert await _stranded(pool) == []


@pytest.mark.asyncio
async def test_retry_refused_for_unsafe_adapter_and_standing_cancel(pool: asyncpg.Pool, monkeypatch) -> None:
    settings = _settings(monkeypatch)
    unsafe = _racing_stack(settings, pool, safe=False)
    command = _envelope(payload={"fixture": "reject"})
    await unsafe.submit(command)
    await unsafe.worker.run_once()
    failed = await unsafe.commands.get(TENANT, command.command_id)
    assert failed.state == "failed"
    with pytest.raises(CommandConflict, match="safe re-execution"):
        await unsafe.platform.kernel.retry(TENANT, command.command_id, principal=_principal(), idempotency_key="retry-pg-unsafe", expected_version=failed.resource_version, reason="retry")
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM middleware_outbox WHERE command_id=$1 AND idempotency_key LIKE 'operation-retry:%'", str(command.command_id)) == 0
    assert (await unsafe.commands.get(TENANT, command.command_id)).resource_version == failed.resource_version

    safe = _racing_stack(settings, pool)
    cancelled = _envelope(payload={"fixture": "reject"})
    await safe.submit(cancelled)
    await safe.worker.run_once()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE middleware_commands SET cancellation_reason='operator cancel' WHERE command_id=$1", str(cancelled.command_id))
    current = await safe.commands.get(TENANT, cancelled.command_id)
    with pytest.raises(CommandConflict, match="cancel"):
        await safe.platform.kernel.retry(TENANT, cancelled.command_id, principal=_principal(), idempotency_key="retry-pg-cancel", expected_version=current.resource_version, reason="retry")
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM middleware_outbox WHERE command_id=$1 AND idempotency_key LIKE 'operation-retry:%'", str(cancelled.command_id)) == 0


@pytest.mark.asyncio
async def test_deferral_at_final_attempt_terminalizes_command_and_intent(pool: asyncpg.Pool, monkeypatch) -> None:
    stack = _racing_stack(_settings(monkeypatch), pool, safe=False, max_attempts=3)
    adapter = stack.test_syn
    adapter.readiness_error = True  # type: ignore[attr-defined]
    command = _envelope()
    await stack.submit(command)
    for _ in range(3):
        async with pool.acquire() as conn:
            await conn.execute("UPDATE middleware_outbox SET next_attempt_at=now() WHERE command_id=$1", str(command.command_id))
        assert await stack.worker.run_once() is True
    rows = await stack.outbox_rows(command.command_id)
    assert rows[0]["dead_lettered_at"] is not None
    assert (await stack.commands.get(TENANT, command.command_id)).state == "dead_lettered"
    record = await stack.commands.get_dead_letter(TENANT, command.command_id)
    assert (record.reason_code, record.attempt_number) == ("deferral_exhausted", 0)
    assert adapter.executed == []
    assert (await stack.commands.backlog(TENANT))[0] == 0
    assert await _stranded(pool) == []


@pytest.mark.asyncio
async def test_deferral_after_reconciliation_requeue_is_not_terminalized(pool: asyncpg.Pool, monkeypatch) -> None:
    """N1 on PostgreSQL: a re-queued command that was once quarantined is not
    dead-lettered "without a provider effect" by a final-attempt deferral."""
    from app.commands import CommandNotFound

    stack = _racing_stack(_settings(monkeypatch), pool, max_attempts=2)
    adapter = stack.test_syn
    reconciler = stack.platform.reconciler
    assert reconciler is not None
    command = _envelope(payload={"fixture": "unknown"})
    await stack.submit(command)
    await stack.worker.run_once()  # outbox attempt 1: unknown outcome, quarantined
    assert (await stack.commands.get(TENANT, command.command_id)).state == "reconciliation_required"
    adapter.effects.pop(str(command.command_id), None)
    await _expire(pool, command.command_id)
    decision = await reconciler.run_once()  # NOT_FOUND + safe adapter: re-queued
    assert decision is not None and decision.final_state == "queued"

    adapter.readiness_error = True  # type: ignore[attr-defined]
    executed_before = list(adapter.executed)
    async with pool.acquire() as conn:
        await conn.execute("UPDATE middleware_outbox SET next_attempt_at=now() WHERE command_id=$1", str(command.command_id))
    assert await stack.worker.run_once() is True  # outbox attempt 2 == max_attempts: readiness fails
    assert (await stack.commands.get(TENANT, command.command_id)).state == "queued"
    with pytest.raises(CommandNotFound):
        await stack.commands.get_dead_letter(TENANT, command.command_id)
    assert adapter.executed == executed_before


@pytest.mark.asyncio
async def test_reexecute_waiver_for_never_sent_dead_letter(pool: asyncpg.Pool, monkeypatch) -> None:
    from app.platform.kernel import ReplayNotAllowed
    from app.platform.resilience import ReplayMode

    stack = _racing_stack(_settings(monkeypatch), pool, safe=False)
    adapter = stack.test_syn
    command = _envelope()
    await stack.submit(command)
    adapter.connector_ids = ("different-connector",)
    await stack.worker.run_once()
    dead = await stack.commands.get(TENANT, command.command_id)
    assert dead.state == "dead_lettered"
    record = await stack.commands.get_dead_letter(TENANT, command.command_id)
    assert (record.reason_code, record.attempt_number) == ("terminal_failure", 0)
    adapter.connector_ids = ("test-syn",)
    replay_key = "idem-pg-" + uuid4().hex  # a fresh REEXECUTE idempotency key
    with pytest.raises(ReplayNotAllowed, match="platform-operator"):
        await stack.platform.kernel.replay(TENANT, command.command_id, principal=_principal(), mode=ReplayMode.REEXECUTE, idempotency_key="replay-pg-0001", expected_version=dead.resource_version, reason="never sent", new_idempotency_key=replay_key)
    replayed = await stack.platform.kernel.replay(TENANT, command.command_id, principal=_operator(), mode=ReplayMode.REEXECUTE, idempotency_key="replay-pg-0001", expected_version=dead.resource_version, reason="never sent", new_idempotency_key=replay_key)
    assert replayed.command_id != command.command_id
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT replay_command_id, state FROM middleware_command_replays WHERE original_command_id=$1", str(command.command_id))
    assert row is not None and row["replay_command_id"] == replayed.command_id and row["state"] == "submitted"
