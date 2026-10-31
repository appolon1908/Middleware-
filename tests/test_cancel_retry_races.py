"""Section 5: cancellation and retry races (memory twin).

Each test drives the real kernel, ExecutionBus, memory outbox and reconciler
(the code the PostgreSQL processes run) and proves the provider effect from
the fixture's provider-side ledger (``FixtureAdapter.effects`` /
``executed``), never from a local state label alone.

D1 cancel ignores the lease; D2 dispatch-boundary cancel checks; D3 the
reconciler honours a persisted cancel; D4 an acknowledgement after a cancel
keeps its provider reference; D5 retry is not a resend path; D6 the REEXECUTE
waiver for proven-not-sent dead letters; D7 outbox-only mutations never strand
the command (B2 deferral budget, B3 reconciler dead-letter record).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

from app.commands import ACTIVE_COMMAND_STATES, CommandConflict, CommandNotFound
from app.core.config import Settings
from app.platform import outcome_codes
from app.platform.adapter import AdapterContext, AdapterReadiness, AdapterResult, ReadbackResult, ReadbackStatus
from app.platform.adapters.fixtures import FixtureAdapter, development_fixtures
from app.platform.bus import BusSettings
from app.platform.kernel import NO_EFFECT_DEAD_LETTER_CODES, ReplayNotAllowed
from app.platform.memory import MemoryExecutionBus
from app.platform.resilience import BulkheadFull, ReplayMode
from tests.test_platform_kernel import TENANT, Harness, envelope, principal

Hook = Callable[[], Awaitable[None]]

OPERATOR = principal(
    roles=("platform-operator",),
    scopes=("platform.command", "platform.command.read", "platform.command.replay"),
)


@dataclass
class RacingAdapter(FixtureAdapter):
    """``test-syn`` with one-shot hooks at the provider boundary.

    ``effects`` is the provider-side ledger: written only when the simulated
    provider accepted a send, before the response returns."""

    before_readiness: Hook | None = None
    after_send: Hook | None = None
    readiness_error: bool = False

    async def readiness(self, context: AdapterContext) -> AdapterReadiness:
        if self.readiness_error:
            raise ConnectionError("provider readiness probe failed")
        hook, self.before_readiness = self.before_readiness, None
        if hook is not None:
            await hook()
        return await super().readiness(context)

    async def execute(self, command: Any, context: AdapterContext) -> AdapterResult:
        result = await super().execute(command, context)
        hook, self.after_send = self.after_send, None
        if hook is not None:
            await hook()
        return result


@dataclass
class ReferenceOnlyAdapter(RacingAdapter):
    """WhatsApp-shaped: no resend safety and a read surface keyed only by the
    provider message id (``whatsapp.py`` returns NOT_FOUND without one)."""

    async def reconcile(self, operation: Any, context: AdapterContext) -> ReadbackResult:
        self.reconciled.append(str(operation.command_id))
        if operation.provider_operation_id and self.effects.get(str(operation.command_id)):
            return ReadbackResult(ReadbackStatus.MATCHED, provider_operation_id=operation.provider_operation_id, evidence={"fixture": "reference"})
        return ReadbackResult(ReadbackStatus.NOT_FOUND, safe_error_code="provider_message_id_missing")


def racing(*, safe: bool = True, cls: type[RacingAdapter] = RacingAdapter) -> RacingAdapter:
    return cls(
        adapter_id="test-syn", provider_family="synthetic", connector_ids=("test-syn",),
        served_capabilities=("TEST_SYN_EXECUTE",), safe_reexecution=safe,
    )


def make(settings: Settings, adapter: RacingAdapter, *, max_attempts: int | None = None) -> Harness:
    bus_settings = BusSettings(max_attempts=max_attempts) if max_attempts is not None else None
    harness = Harness(settings, adapters=(adapter, *development_fixtures()[1:]), bus_settings=bus_settings)
    if max_attempts is not None:
        harness.bus = MemoryExecutionBus(harness.store, harness.dispatch, max_attempts=max_attempts)
    return harness


async def cancel(harness: Harness, command_id: UUID, key: str) -> Any:
    current = await harness.commands.get(TENANT, command_id)
    return await harness.kernel.cancel(
        TENANT, command_id, principal=principal(), idempotency_key=key,
        expected_version=current.resource_version, reason="operator cancel",
    )


def assert_no_stranded_command(harness: Harness) -> None:
    """No non-terminal command whose every outbox intent is terminal."""
    for (_, command_id), (_, operation) in harness.store._commands.items():
        if operation.state in (*ACTIVE_COMMAND_STATES, "reconciliation_required"):
            intents = harness.intents(command_id)
            assert any(
                item.completed_at is None and item.dead_lettered_at is None and item.cancelled_at is None
                for item in intents
            ), f"command {command_id} is {operation.state} with no live outbox intent"


def make_claimable(harness: Harness, command_id: UUID) -> None:
    for item in harness.intents(command_id):
        item.next_attempt_at = 0.0


# ----------------------------------------------------------------------------
# D1 + D2: cancel before the provider call is a real cancel
# ----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_cancel_before_dispatch_with_live_lease_is_cancelled(test_settings: Settings) -> None:
    adapter = racing()
    harness = make(test_settings, adapter)
    command = envelope()
    await harness.submit(command)
    outcome: dict[str, Any] = {}

    async def cancel_while_leased() -> None:
        assert harness.intents(command.command_id)[0].lease_owner is not None
        outcome["cancel"] = await cancel(harness, command.command_id, "cancel-live-lease")

    adapter.before_readiness = cancel_while_leased
    await harness.bus.run_once()
    assert outcome["cancel"].state == "cancelled" and outcome["cancel"].cancelled_at is not None

    harness.bus.expire_leases()
    await harness.reconciler.run_once()
    await harness.bus.drain()
    assert adapter.executed == [] and adapter.provider_effects == 0
    assert (await harness.commands.get(TENANT, command.command_id)).state == "cancelled"
    assert_no_stranded_command(harness)


@pytest.mark.asyncio
async def test_concurrent_cancel_with_safe_retry_route(test_settings: Settings) -> None:
    adapter = racing()
    harness = make(test_settings, adapter)
    command = envelope()
    await harness.submit(command)

    async def cancel_then_go_unready() -> None:
        await cancel(harness, command.command_id, "cancel-unready")
        adapter.ready = False

    adapter.before_readiness = cancel_then_go_unready
    await harness.bus.run_once()  # readiness denial: a known-safe deferral
    assert harness.bus.processed[-1][1] == "retry"
    adapter.ready = True
    make_claimable(harness, command.command_id)
    await harness.bus.run_once()
    assert harness.dispatch.last_outcome is not None and harness.dispatch.last_outcome.final_state == "cancelled"
    assert adapter.executed == [] and adapter.provider_effects == 0
    assert (await harness.commands.get(TENANT, command.command_id)).state == "cancelled"
    assert_no_stranded_command(harness)


@pytest.mark.asyncio
async def test_cancel_with_expired_lease_is_cancelled(test_settings: Settings) -> None:
    harness = make(test_settings, racing())
    command = envelope()
    await harness.submit(command)
    intent = harness.intents(command.command_id)[0]
    intent.lease_owner, intent.lease_until = "dead-worker", harness.bus.clock() - 10.0
    cancelled = await cancel(harness, command.command_id, "cancel-expired")
    assert cancelled.state == "cancelled" and cancelled.cancelled_at is not None


@pytest.mark.asyncio
async def test_cancel_wins_dispatching_transition_returns_cancelled_outcome(test_settings: Settings) -> None:
    adapter = racing()
    harness = make(test_settings, adapter)
    command = envelope()
    await harness.submit(command)
    await harness.commands.transition(TENANT, command.command_id, new_state="queued", actor_id="w", reason="queued")

    async def cancel_before_attempt() -> None:
        await cancel(harness, command.command_id, "cancel-queued")

    adapter.before_readiness = cancel_before_attempt
    await harness.bus.run_once()
    outcome = harness.dispatch.last_outcome
    assert outcome is not None and outcome.final_state == "cancelled" and outcome.attempt is None
    intent = harness.intents(command.command_id)[0]
    assert intent.completed_at is not None and intent.reconciliation_required_at is None
    assert await harness.commands.latest_attempt(TENANT, command.command_id) == 0
    assert adapter.executed == []
    assert_no_stranded_command(harness)


async def _cancel_between_attempt_open_and_send(harness: Harness, command_id: UUID) -> None:
    original = harness.store.latest_attempt
    fired = False

    async def latest_attempt_then_cancel(tenant_id: str, cid: UUID) -> int:
        nonlocal fired
        value = await original(tenant_id, cid)
        if not fired and cid == command_id:
            fired = True
            ambiguous = await cancel(harness, command_id, "cancel-open-attempt")
            assert ambiguous.state == "reconciliation_required" and ambiguous.cancelled_at is None
        return value

    harness.store.latest_attempt = latest_attempt_then_cancel  # type: ignore[method-assign]
    try:
        await harness.bus.run_once()
    finally:
        harness.store.latest_attempt = original  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_cancel_between_attempt_open_and_send_never_sends(test_settings: Settings) -> None:
    adapter = racing()
    harness = make(test_settings, adapter)
    command = envelope()
    await harness.submit(command)
    await _cancel_between_attempt_open_and_send(harness, command.command_id)

    assert adapter.executed == [] and adapter.provider_effects == 0
    assert (await harness.commands.get(TENANT, command.command_id)).state == "dead_lettered"
    record = await harness.commands.get_dead_letter(TENANT, command.command_id)
    assert record.reason_code == "cancel_requested_provider_missing"
    assert record.error_class == "cancelled_before_send"
    assert record.attempt_number == 1
    await harness.bus.drain()
    assert adapter.executed == []
    assert_no_stranded_command(harness)


# ----------------------------------------------------------------------------
# D3 + D4: cancel after the provider call
# ----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_cancel_after_transmission_effect_completes(test_settings: Settings) -> None:
    adapter = racing()
    harness = make(test_settings, adapter)
    command = envelope()
    await harness.submit(command)
    seen: dict[str, Any] = {}

    async def cancel_after_send() -> None:
        seen["cancel"] = await cancel(harness, command.command_id, "cancel-after-send")

    adapter.after_send = cancel_after_send
    await harness.bus.run_once()
    assert seen["cancel"].state == "reconciliation_required" and seen["cancel"].cancelled_at is None
    parked = await harness.commands.get(TENANT, command.command_id)
    assert parked.state == "reconciliation_required"
    assert parked.provider_operation_id == f"test-syn:{command.command_id}:1"

    harness.bus.expire_leases()
    decision = await harness.reconciler.run_once()
    assert decision is not None and decision.final_state == "completed"
    assert adapter.effects[str(command.command_id)] == 1
    events = await harness.commands.list_events(TENANT, command.command_id, limit=50)
    assert "cancelled" not in {event.new_state for event in events}
    assert_no_stranded_command(harness)


@pytest.mark.asyncio
async def test_cancel_after_transmission_without_effect_dead_letters(test_settings: Settings) -> None:
    adapter = racing()
    harness = make(test_settings, adapter)
    command = envelope(payload={"fixture": "unknown"})
    await harness.submit(command)

    async def cancel_and_provider_loses_it() -> None:
        await cancel(harness, command.command_id, "cancel-unknown")
        adapter.effects.pop(str(command.command_id), None)

    adapter.after_send = cancel_and_provider_loses_it
    await harness.bus.run_once()
    harness.bus.expire_leases()
    decision = await harness.reconciler.run_once()
    assert decision is not None and decision.final_state == "dead_lettered"
    record = await harness.commands.get_dead_letter(TENANT, command.command_id)
    assert record.reason_code == "cancel_requested_provider_missing"
    assert record.error_class != "cancelled_before_send"
    await harness.bus.drain()
    assert len(adapter.executed) == 1  # never re-queued on a standing cancel
    assert_no_stranded_command(harness)


@pytest.mark.asyncio
async def test_ack_after_cancel_keeps_provider_reference(test_settings: Settings) -> None:
    adapter = racing(safe=False, cls=ReferenceOnlyAdapter)
    harness = make(test_settings, adapter)
    command = envelope()
    await harness.submit(command)

    async def cancel_after_send() -> None:
        await cancel(harness, command.command_id, "cancel-ack")

    adapter.after_send = cancel_after_send
    await harness.bus.run_once()
    parked = await harness.commands.get(TENANT, command.command_id)
    assert parked.state == "reconciliation_required"
    assert parked.provider_operation_id == f"test-syn:{command.command_id}:1"
    assert parked.readback_evidence is not None and parked.readback_evidence["status"] == "acknowledged_after_cancel"

    harness.bus.expire_leases()
    decision = await harness.reconciler.run_once()
    assert decision is not None and decision.final_state == "completed"
    assert adapter.effects[str(command.command_id)] == 1 and len(adapter.executed) == 1


# ----------------------------------------------------------------------------
# D5: retry is not a resend path
# ----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_retry_refused_for_non_reexecutable_adapter(test_settings: Settings) -> None:
    adapter = racing(safe=False)
    harness = make(test_settings, adapter)
    command = envelope(payload={"fixture": "reject"})
    await harness.submit(command)
    await harness.bus.run_once()
    failed = await harness.commands.get(TENANT, command.command_id)
    assert failed.state == "failed"
    with pytest.raises(CommandConflict, match="safe re-execution"):
        await harness.kernel.retry(
            TENANT, command.command_id, principal=principal(), idempotency_key="retry-unsafe",
            expected_version=failed.resource_version, reason="retry",
        )
    after = await harness.commands.get(TENANT, command.command_id)
    assert after.state == "failed" and after.resource_version == failed.resource_version
    assert len(harness.intents(command.command_id)) == 1
    await harness.bus.drain()
    assert len(adapter.executed) == 1


@pytest.mark.asyncio
async def test_retry_refused_when_cancel_requested(test_settings: Settings) -> None:
    harness = make(test_settings, racing())
    command = envelope(payload={"fixture": "reject"})
    await harness.submit(command)
    await harness.bus.run_once()
    key = (TENANT, command.command_id)
    digest, failed = harness.store._commands[key]
    assert failed.state == "failed"
    harness.store._commands[key] = (digest, failed.model_copy(update={"cancellation_reason": "operator cancel"}))
    with pytest.raises(CommandConflict, match="cancel"):
        await harness.kernel.retry(
            TENANT, command.command_id, principal=principal(), idempotency_key="retry-cancelled",
            expected_version=failed.resource_version, reason="retry",
        )
    after = await harness.commands.get(TENANT, command.command_id)
    assert after.state == "failed" and after.cancellation_reason == "operator cancel"


@pytest.mark.asyncio
async def test_memory_retry_refuses_dead_lettered(test_settings: Settings) -> None:
    adapter = racing()
    harness = make(test_settings, adapter)
    command = envelope()
    await harness.submit(command)
    adapter.connector_ids = ("different-connector",)
    await harness.bus.drain()
    dead = await harness.commands.get(TENANT, command.command_id)
    assert dead.state == "dead_lettered"
    with pytest.raises(CommandConflict, match="not safely retryable"):
        await harness.kernel.retry(
            TENANT, command.command_id, principal=principal(), idempotency_key="retry-dead",
            expected_version=dead.resource_version, reason="retry",
        )


@pytest.mark.asyncio
async def test_store_retry_defaults_fail_closed_for_adapter_commands(test_settings: Settings) -> None:
    """The compatibility route calls the store directly: without the kernel's
    registry fact an adapter command is never re-executed."""
    harness = make(test_settings, racing())
    command = envelope(payload={"fixture": "reject"})
    await harness.submit(command)
    await harness.bus.run_once()
    failed = await harness.commands.get(TENANT, command.command_id)
    with pytest.raises(CommandConflict, match="safe re-execution"):
        await harness.commands.mutate_operation(
            TENANT, command.command_id, action="retry", actor_id="compat", idempotency_key="retry-compat",
            expected_version=failed.resource_version, reason="retry",
        )
    queued = await harness.commands.mutate_operation(
        TENANT, command.command_id, action="retry", actor_id="kernel", idempotency_key="retry-kernel",
        expected_version=failed.resource_version, reason="retry", reexecution_safe=True,
    )
    assert queued.state == "queued" and len(harness.intents(command.command_id)) == 2


# ----------------------------------------------------------------------------
# D6: REEXECUTE waiver for proven-not-sent dead letters
# ----------------------------------------------------------------------------
async def _reexecute(harness: Harness, command_id: UUID, *, who: Any = OPERATOR, key: str = "replay-waiver", new_key: str | None = "idem-waiver-0000001") -> Any:
    current = await harness.commands.get(TENANT, command_id)
    return await harness.kernel.replay(
        TENANT, command_id, principal=who, mode=ReplayMode.REEXECUTE, idempotency_key=key,
        expected_version=current.resource_version, reason="never sent", new_idempotency_key=new_key,
    )


@pytest.mark.asyncio
async def test_reexecute_waiver_no_effect_terminal_failure(test_settings: Settings) -> None:
    adapter = racing(safe=False)
    harness = make(test_settings, adapter)
    command = envelope()
    await harness.submit(command)
    adapter.connector_ids = ("different-connector",)
    await harness.bus.drain()
    record = await harness.commands.get_dead_letter(TENANT, command.command_id)
    assert (record.reason_code, record.attempt_number) == ("terminal_failure", 0)
    adapter.connector_ids = ("test-syn",)

    with pytest.raises(ReplayNotAllowed, match="platform-operator"):
        await _reexecute(harness, command.command_id, who=principal())
    with pytest.raises(ReplayNotAllowed, match="new idempotency key"):
        await _reexecute(harness, command.command_id, new_key=command.idempotency_key)
    replayed = await _reexecute(harness, command.command_id)
    assert replayed.command_id != command.command_id
    assert len(harness.store._outbox) == 2

    sent = envelope(payload={"fixture": "reject"})
    await harness.submit(sent)
    await harness.bus.drain()
    rejected = await harness.commands.get_dead_letter(TENANT, sent.command_id)
    assert rejected.attempt_number == 1
    with pytest.raises(ReplayNotAllowed, match="safe re-execution"):
        await _reexecute(harness, sent.command_id, key="replay-sent", new_key="idem-waiver-0000002")


@pytest.mark.asyncio
async def test_reexecute_waiver_covers_pre_send_cancel(test_settings: Settings) -> None:
    adapter = racing(safe=False)
    harness = make(test_settings, adapter)
    command = envelope()
    await harness.submit(command)
    await _cancel_between_attempt_open_and_send(harness, command.command_id)
    assert adapter.executed == []
    replayed = await _reexecute(harness, command.command_id)
    assert replayed.command_id != command.command_id


@pytest.mark.asyncio
async def test_reexecute_waiver_refuses_pre_send_cancel_after_an_earlier_send(test_settings: Settings) -> None:
    """N2: a pre-send cancel on attempt 2 does not prove attempt 1 sent nothing."""
    adapter = racing()  # safe, so the reconciler re-queues after NOT_FOUND
    harness = make(test_settings, adapter)
    command = envelope()
    adapter.scripts[str(command.command_id)] = ["unknown"]
    await harness.submit(command)
    await harness.bus.run_once()  # attempt 1 reaches the provider; outcome unknown
    assert adapter.effects[str(command.command_id)] == 1
    adapter.effects.pop(str(command.command_id))  # the provider's record is lost
    harness.bus.expire_leases()
    decision = await harness.reconciler.run_once()
    assert decision is not None and decision.final_state == "queued"
    make_claimable(harness, command.command_id)
    await _cancel_between_attempt_open_and_send(harness, command.command_id)
    record = await harness.commands.get_dead_letter(TENANT, command.command_id)
    assert (record.reason_code, record.error_class, record.attempt_number) == (
        "cancel_requested_provider_missing", "cancelled_before_send", 2,
    )
    assert [item for item in adapter.executed if item[0] == str(command.command_id)] == [(str(command.command_id), 1)]

    # The adapter is not safely re-executable: only the waiver could allow REEXECUTE.
    registry = harness.kernel.registry
    unsafe = replace(registry.advertised("test-syn"), safe_reexecution=False)
    original_advertised = registry.advertised
    registry.advertised = lambda adapter_id: unsafe if adapter_id == "test-syn" else original_advertised(adapter_id)  # type: ignore[method-assign]
    with pytest.raises(ReplayNotAllowed, match="safe re-execution"):
        await _reexecute(harness, command.command_id)


@pytest.mark.asyncio
async def test_reexecute_waiver_refuses_cancel_requested_and_provider_missing(test_settings: Settings) -> None:
    adapter = racing(safe=False)
    harness = make(test_settings, adapter)

    cancelled = envelope(payload={"fixture": "unknown"})
    await harness.submit(cancelled)

    async def cancel_and_lose() -> None:
        await cancel(harness, cancelled.command_id, "cancel-unsafe")
        adapter.effects.pop(str(cancelled.command_id), None)

    adapter.after_send = cancel_and_lose
    await harness.bus.run_once()
    harness.bus.expire_leases()
    await harness.reconciler.run_once()
    record = await harness.commands.get_dead_letter(TENANT, cancelled.command_id)
    assert record.reason_code == "cancel_requested_provider_missing" and record.error_class != "cancelled_before_send"
    with pytest.raises(ReplayNotAllowed, match="safe re-execution"):
        await _reexecute(harness, cancelled.command_id)

    missing = envelope(payload={"fixture": "unknown"})
    await harness.submit(missing)
    await harness.bus.run_once()
    adapter.effects.pop(str(missing.command_id), None)
    harness.bus.expire_leases()
    await harness.reconciler.run_once()
    assert (await harness.commands.get_dead_letter(TENANT, missing.command_id)).reason_code == "provider_missing_manual_repair"
    with pytest.raises(ReplayNotAllowed, match="safe re-execution"):
        await _reexecute(harness, missing.command_id, key="replay-missing", new_key="idem-waiver-0000003")


def test_waiver_predicate_is_closed_and_excludes_poisoned() -> None:
    assert NO_EFFECT_DEAD_LETTER_CODES is outcome_codes.NO_EFFECT_DEAD_LETTER_CODES
    assert NO_EFFECT_DEAD_LETTER_CODES == {"effect_gate_denied", "deferral_exhausted", "dispatch_age_exceeded", "terminal_failure"}

    def facts(**updates: Any) -> Any:
        values = {"attempt_number": 0, "reason_code": "terminal_failure", "error_class": "non_retryable", "poisoned": False}
        values.update(updates)
        return SimpleNamespace(**values)

    assert outcome_codes.proven_not_sent(facts(), latest_attempt=0)
    assert not outcome_codes.proven_not_sent(facts(poisoned=True), latest_attempt=0)
    assert not outcome_codes.proven_not_sent(facts(), latest_attempt=1)
    assert not outcome_codes.proven_not_sent(facts(attempt_number=1), latest_attempt=1)
    assert not outcome_codes.proven_not_sent(facts(reason_code="retry_exhausted"), latest_attempt=0)
    pre_send = facts(attempt_number=1, reason_code="cancel_requested_provider_missing", error_class="cancelled_before_send")
    assert outcome_codes.proven_not_sent(pre_send, latest_attempt=1)
    assert not outcome_codes.proven_not_sent(pre_send, latest_attempt=2)
    later_pre_send = facts(attempt_number=2, reason_code="cancel_requested_provider_missing", error_class="cancelled_before_send")
    assert not outcome_codes.proven_not_sent(later_pre_send, latest_attempt=2)  # attempt 1 may have sent
    assert not outcome_codes.proven_not_sent(facts(attempt_number=1, reason_code="cancel_requested_provider_missing", error_class="not_found"), latest_attempt=1)
    assert outcome_codes.DEAD_LETTER_REASON_CODES >= NO_EFFECT_DEAD_LETTER_CODES | {"reconciliation_dead_letter_unrecorded"}


# ----------------------------------------------------------------------------
# D7: outbox-only mutations never strand the command
# ----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_deferral_at_final_attempt_dead_letters_command_and_intent(test_settings: Settings) -> None:
    adapter = racing(safe=False)
    adapter.readiness_error = True
    harness = make(test_settings, adapter, max_attempts=3)
    command = envelope()
    await harness.submit(command)
    for _ in range(3):
        assert (await harness.commands.backlog(TENANT))[0] == 1
        make_claimable(harness, command.command_id)
        assert await harness.bus.run_once()
    intent = harness.intents(command.command_id)[0]
    assert intent.dead_lettered_at is not None
    assert (await harness.commands.get(TENANT, command.command_id)).state == "dead_lettered"
    record = await harness.commands.get_dead_letter(TENANT, command.command_id)
    assert (record.reason_code, record.attempt_number) == ("deferral_exhausted", 0)
    assert (await harness.commands.backlog(TENANT))[0] == 0
    assert adapter.executed == []
    assert_no_stranded_command(harness)
    adapter.readiness_error = False
    replayed = await _reexecute(harness, command.command_id)  # deferral_exhausted at attempt 0 is waivable
    assert replayed.command_id != command.command_id


@pytest.mark.asyncio
async def test_bulkhead_exhaustion_dead_letters_with_retry_exhausted(test_settings: Settings) -> None:
    adapter = racing()
    harness = make(test_settings, adapter, max_attempts=3)

    class FullBulkhead:
        async def run(self, operation: Any) -> Any:
            raise BulkheadFull("bulkhead test-syn saturated (1)")

    harness.dispatch.bulkhead = lambda adapter_id: FullBulkhead()  # type: ignore[method-assign,assignment,return-value]
    command = envelope()
    await harness.submit(command)
    for _ in range(3):
        make_claimable(harness, command.command_id)
        assert await harness.bus.run_once()
    assert harness.intents(command.command_id)[0].dead_lettered_at is not None
    assert (await harness.commands.get(TENANT, command.command_id)).state == "dead_lettered"
    record = await harness.commands.get_dead_letter(TENANT, command.command_id)
    assert record.reason_code == "retry_exhausted" and record.attempt_number >= 1
    assert adapter.executed == []
    assert_no_stranded_command(harness)


@pytest.mark.asyncio
async def test_deferral_after_reconciliation_requeue_is_not_terminalized(test_settings: Settings) -> None:
    """N1: a command that was quarantined (reconciliation_required) and then
    re-queued must never be dead-lettered as exhausted "without a provider
    effect" by a readiness deferral on the final outbox attempt."""
    adapter = racing()
    harness = make(test_settings, adapter, max_attempts=2)
    command = envelope(payload={"fixture": "unknown"})
    await harness.submit(command)
    await harness.bus.run_once()  # outbox attempt 1: unknown outcome, quarantined
    assert (await harness.commands.get(TENANT, command.command_id)).state == "reconciliation_required"
    adapter.effects.pop(str(command.command_id), None)
    harness.bus.expire_leases()
    decision = await harness.reconciler.run_once()  # NOT_FOUND + safe adapter: re-queued
    assert decision is not None and decision.final_state == "queued"
    assert (await harness.commands.get(TENANT, command.command_id)).state == "queued"

    adapter.readiness_error = True
    make_claimable(harness, command.command_id)
    executed_before = list(adapter.executed)
    assert await harness.bus.run_once()  # outbox attempt 2 == max_attempts: readiness fails
    assert harness.intents(command.command_id)[0].attempt_count == 2
    operation = await harness.commands.get(TENANT, command.command_id)
    assert operation.state == "queued"  # left for reconciliation, not terminalized
    with pytest.raises(CommandNotFound):
        await harness.commands.get_dead_letter(TENANT, command.command_id)
    assert adapter.executed == executed_before


@pytest.mark.asyncio
async def test_deferral_of_queued_command_at_final_attempt_still_terminalizes(test_settings: Settings) -> None:
    """N1 (b): a queued command whose only history is classified no-effect
    closes keeps the B2 budget: deferral_exhausted at attempt 0,
    retry_exhausted after a known-safe opened attempt."""
    adapter = racing()
    harness = make(test_settings, adapter, max_attempts=2)
    never_opened = envelope()
    await harness.submit(never_opened)
    await harness.commands.transition(TENANT, never_opened.command_id, new_state="queued", actor_id="w", reason="queued")
    adapter.readiness_error = True
    for _ in range(2):
        make_claimable(harness, never_opened.command_id)
        assert await harness.bus.run_once()
    record = await harness.commands.get_dead_letter(TENANT, never_opened.command_id)
    assert (record.reason_code, record.attempt_number) == ("deferral_exhausted", 0)
    assert (await harness.commands.get(TENANT, never_opened.command_id)).state == "dead_lettered"

    adapter.readiness_error = False
    transient = envelope()
    adapter.scripts[str(transient.command_id)] = ["transient"]
    await harness.submit(transient)
    assert await harness.bus.run_once()  # attempt 1 closes known-safe; command back to queued
    assert (await harness.commands.get(TENANT, transient.command_id)).state == "queued"
    adapter.readiness_error = True
    make_claimable(harness, transient.command_id)
    assert await harness.bus.run_once()
    record = await harness.commands.get_dead_letter(TENANT, transient.command_id)
    assert (record.reason_code, record.attempt_number) == ("retry_exhausted", 1)
    assert (await harness.commands.get(TENANT, transient.command_id)).state == "dead_lettered"
    assert adapter.provider_effects == 0
    assert_no_stranded_command(harness)


@pytest.mark.asyncio
async def test_reconciler_connector_unavailable_writes_dead_letter_record(test_settings: Settings) -> None:
    adapter = racing()
    harness = make(test_settings, adapter)
    command = envelope(payload={"fixture": "unknown"})
    await harness.submit(command)
    await harness.bus.run_once()
    adapter.connector_ids = ("different-connector",)
    harness.bus.expire_leases()
    decision = await harness.reconciler.run_once()
    assert decision is not None and decision.final_state == "dead_lettered"
    record = await harness.commands.get_dead_letter(TENANT, command.command_id)
    assert record.reason_code == "reconciliation_dead_letter_unrecorded"
    assert record.error_class == "configuration" and record.attempt_number == 1
