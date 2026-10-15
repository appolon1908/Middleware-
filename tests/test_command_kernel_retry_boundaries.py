"""Retry budgets and operator reexecution remain durable and idempotent."""

import asyncio

import pytest

from app.platform.resilience import ReplayMode
from tests.test_platform_kernel import Harness, TENANT, envelope, principal


@pytest.mark.asyncio
@pytest.mark.parametrize("stalled", [False, True])
async def test_readiness_probe_failure_uses_bounded_safe_retry(
    test_settings, monkeypatch, stalled
):
    harness = Harness(test_settings)
    harness.dispatch.bus.max_attempts = harness.bus.max_attempts = 2
    harness.dispatch.bus.default_timeout_seconds = 0.01
    command = envelope()
    await harness.submit(command)

    async def broken_readiness(context):
        if stalled:
            await asyncio.Event().wait()
        raise RuntimeError("probe failed")

    monkeypatch.setattr(harness.test_syn, "readiness", broken_readiness)
    assert await asyncio.wait_for(harness.bus.run_once(), timeout=2)
    intent = harness.intents(command.command_id)[0]
    assert intent.reconciliation_required_at is None
    assert intent.next_attempt_at > harness.bus.clock()
    assert (await harness.commands.get(TENANT, command.command_id)).state == "persisted"
    intent.next_attempt_at = 0
    assert await asyncio.wait_for(harness.bus.run_once(), timeout=2)
    assert intent.dead_lettered_at is not None
    assert (
        await harness.commands.get(TENANT, command.command_id)
    ).state == "dead_lettered"
    assert harness.test_syn.executed == []
    assert await harness.commands.latest_attempt(TENANT, command.command_id) == 0


@pytest.mark.asyncio
async def test_readiness_exhaustion_dead_letters_ledger_and_outbox(test_settings):
    harness = Harness(test_settings)
    harness.dispatch.bus.max_attempts = harness.bus.max_attempts = 2
    command = envelope()
    await harness.submit(command)
    harness.test_syn.ready = False
    assert await harness.bus.run_once()
    intent = harness.intents(command.command_id)[0]
    intent.next_attempt_at = 0
    assert await harness.bus.run_once()
    assert intent.dead_lettered_at is not None
    assert (
        await harness.commands.get(TENANT, command.command_id)
    ).state == "dead_lettered"
    assert harness.test_syn.executed == []


@pytest.mark.asyncio
async def test_reexecution_request_retry_returns_same_new_operation(test_settings):
    harness = Harness(test_settings)
    command = envelope(payload={"fixture": "reject"})
    await harness.submit(command)
    await harness.bus.run_once()
    original = await harness.commands.get(TENANT, command.command_id)
    operator = principal(
        roles=("platform-operator",),
        scopes=("platform.command", "platform.command.replay"),
    )
    arguments = dict(
        principal=operator,
        mode=ReplayMode.REEXECUTE,
        idempotency_key="replay-request-key",
        expected_version=original.resource_version,
        reason="safe retry",
        new_idempotency_key="new-execution-key",
    )
    first = await harness.kernel.replay(TENANT, command.command_id, **arguments)
    second = await harness.kernel.replay(TENANT, command.command_id, **arguments)
    assert second.command_id == first.command_id
    assert second.duplicate
    assert len(harness.store._outbox) == 2


@pytest.mark.asyncio
async def test_not_found_after_dispatch_budget_is_dead_lettered(test_settings):
    harness = Harness(test_settings)
    command = envelope(payload={"fixture": "unknown"})
    await harness.submit(command)
    await harness.bus.run_once()
    intent = harness.intents(command.command_id)[0]
    intent.attempt_count = harness.bus.max_attempts
    harness.test_syn.effects.clear()
    harness.bus.expire_leases()
    decision = await harness.reconciler.run_once()
    assert decision.final_state == "dead_lettered"
    assert intent.dead_lettered_at is not None
    assert (
        await harness.commands.get(TENANT, command.command_id)
    ).state == "dead_lettered"
