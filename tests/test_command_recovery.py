from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from app.commands import CommandNotFound
from app.platform.resilience import ReplayMode
from tests.test_platform_kernel import Harness, TENANT, envelope, principal


@pytest.fixture
def harness(test_settings):
    return Harness(test_settings)


@pytest.mark.asyncio
async def test_terminal_failure_creates_dead_letter_with_lineage(harness: Harness) -> None:
    command = envelope(payload={"fixture": "reject"})
    await harness.submit(command)
    await harness.bus.run_once()
    record = await harness.commands.get_dead_letter(TENANT, command.command_id)
    assert record.command_id == command.command_id
    assert record.correlation_id == command.correlation_id
    assert record.principal_id == command.requested_by
    assert record.capability == command.capability
    assert record.error_class == "non_retryable"
    assert record.attempt_number == 1


@pytest.mark.asyncio
async def test_replay_is_idempotent_and_preserves_original_history(harness: Harness) -> None:
    command = envelope(payload={"fixture": "reject"})
    await harness.submit(command)
    await harness.bus.run_once()
    operator = principal(
        roles=("platform-operator",),
        scopes=("platform.command", "platform.command.read", "platform.command.replay"),
    )
    first, second = await asyncio.gather(
        harness.kernel.replay(
            TENANT, command.command_id, principal=operator, mode=ReplayMode.REEXECUTE,
            idempotency_key="replay-concurrent-0001", expected_version=1, reason="recover",
            new_idempotency_key="new-effect-00000001",
        ),
        harness.kernel.replay(
            TENANT, command.command_id, principal=operator, mode=ReplayMode.REEXECUTE,
            idempotency_key="replay-concurrent-0001", expected_version=1, reason="recover",
            new_idempotency_key="new-effect-00000001",
        ),
    )
    assert first.command_id == second.command_id
    assert first.command_id != command.command_id
    history = await harness.commands.list_replays(TENANT, command.command_id)
    assert len(history) == 1
    assert history[0].replay_command_id == first.command_id
    attempts = await harness.commands.list_attempts(TENANT, command.command_id, limit=10)
    assert len(attempts) == 1


@pytest.mark.asyncio
async def test_recovery_is_tenant_isolated(harness: Harness) -> None:
    command = envelope(payload={"fixture": "reject"})
    await harness.submit(command)
    await harness.bus.run_once()
    other = "OTHER_SYN"
    with pytest.raises(CommandNotFound):
        await harness.commands.get_dead_letter(other, command.command_id)
    assert await harness.commands.list_dead_letters(other) == []


@pytest.mark.asyncio
async def test_replay_can_be_cancelled_before_submission(harness: Harness) -> None:
    command = envelope(payload={"fixture": "reject"})
    await harness.submit(command)
    await harness.bus.run_once()
    replay_id = uuid4()
    row = await harness.commands.create_replay(
        TENANT, command.command_id, replay_id=replay_id, actor_id="operator",
        idempotency_key="scheduled-replay-0001", reason="scheduled recovery",
    )
    assert row.state == "requested"
    cancelled = await harness.commands.cancel_replay(TENANT, replay_id, actor_id="operator")
    assert cancelled.state == "cancelled"
    with pytest.raises(Exception, match="cancelled replay cannot execute"):
        await harness.commands.complete_replay(TENANT, replay_id, uuid4())


@pytest.mark.asyncio
async def test_retry_exhaustion_marks_dead_letter(harness: Harness) -> None:
    harness.bus.max_attempts = 1
    harness.dispatch.bus.max_attempts = 1
    command = envelope()
    harness.test_syn.scripts[str(command.command_id)] = ["transient"]
    await harness.submit(command)
    await harness.bus.run_once()
    operation = await harness.commands.get(TENANT, command.command_id)
    assert operation.state == "dead_lettered"
    record = await harness.commands.get_dead_letter(TENANT, command.command_id)
    assert record.retry_exhausted is True
    assert record.reason_code == "retry_exhausted"
