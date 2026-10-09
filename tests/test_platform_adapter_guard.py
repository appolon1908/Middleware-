"""The registry's adapter guard: whatever an adapter returns, the kernel only
sees the normalised contract; a provider is never invoked for a command it
does not own, for another tenant, or while its capability is off."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, cast
from uuid import uuid4

import pytest

from app.commands import CommandEnvelope, CommandOperation, CommandPolicy, CommandPolicyRegistry
from app.platform.adapter import (
    AdapterContext,
    AdapterReadiness,
    AdapterResult,
    BaseAdapter,
    ErrorClass,
    Outcome,
    ReadbackResult,
    ReadbackStatus,
)
from app.platform.conformance import (
    CONTRACT_VIOLATIONS,
    GUARD_REFUSALS,
    ConformingAdapter,
    conform_readback,
    conform_result,
    safe_error_code,
    safe_mapping,
    safe_operation_id,
    unwrap,
)
from app.platform.registry import AdapterRegistry

PREFIX = "guard.probe."
TARGET = "guard-probe"
CAPABILITY = "GUARD_PROBE_WRITE"


def policies(*, enabled: bool) -> CommandPolicyRegistry:
    return CommandPolicyRegistry(
        (
            CommandPolicy(prefix=PREFIX, target=TARGET, capability=CAPABILITY, readback_required=True),
            CommandPolicy(prefix="other.", target="other-target", capability="OTHER_WRITE", readback_required=True),
        ),
        {CAPABILITY: enabled, "OTHER_WRITE": True},
    )


@dataclass
class ScriptedAdapter(BaseAdapter):
    adapter_id: str = "guard-probe"
    provider_family: str = "probe"
    connector_ids: tuple[str, ...] = (TARGET,)
    served_capabilities: tuple[str, ...] = (CAPABILITY,)
    supports_cancel: bool = True
    external_effect: bool = True
    answer: Any = None
    readback_answer: Any = None
    cancel_answer: Any = None
    calls: list[str] = field(default_factory=list)

    async def execute(self, command: CommandEnvelope, context: AdapterContext) -> AdapterResult:
        self.calls.append("execute")
        return self.answer

    async def readback(self, operation: CommandOperation, context: AdapterContext) -> ReadbackResult:
        self.calls.append("readback")
        return self.readback_answer

    async def cancel(self, operation: CommandOperation, context: AdapterContext) -> AdapterResult:
        self.calls.append("cancel")
        return self.cancel_answer


def envelope(**updates: Any) -> CommandEnvelope:
    value: dict[str, Any] = {
        "command_id": str(uuid4()),
        "command_type": PREFIX + "write.v1",
        "command_version": "1.0",
        "target": TARGET,
        "tenant_id": "tenant-a",
        "requested_by": "user-1",
        "correlation_id": "corr-1",
        "idempotency_key": "idem-" + uuid4().hex,
        "capability": CAPABILITY,
        "payload": {"probe": True},
    }
    value.update(updates)
    return CommandEnvelope.model_validate(value)


def context(command: CommandEnvelope, **updates: Any) -> AdapterContext:
    values: dict[str, Any] = {
        "tenant_id": command.tenant_id,
        "command_id": str(command.command_id),
        "correlation_id": command.correlation_id,
        "attempt": 1,
        "timeout_seconds": 5.0,
        "environment": "test",
        "deployment_sha": "0" * 40,
        "payload": command.payload,
    }
    values.update(updates)
    return AdapterContext(**values)


def operation(command: CommandEnvelope, **updates: Any) -> CommandOperation:
    now = datetime.now(timezone.utc)
    values = {**command.model_dump(exclude={"payload"}), "state": "accepted", "created_at": now, "updated_at": now}
    values.update(updates)
    return CommandOperation(**values)


def guarded(adapter: ScriptedAdapter, *, enabled: bool = True) -> ConformingAdapter:
    registry = AdapterRegistry(policies(enabled=enabled))
    result = registry.register(adapter)
    assert isinstance(result, ConformingAdapter)
    return result


def violations(kind: str, adapter_id: str = "guard-probe") -> float:
    return CONTRACT_VIOLATIONS.labels(adapter=adapter_id, kind=kind)._value.get()


def refusals(reason: str, adapter_id: str = "guard-probe") -> float:
    return GUARD_REFUSALS.labels(adapter=adapter_id, reason=reason)._value.get()


# ----------------------------------------------------------------------------
# registration
# ----------------------------------------------------------------------------
def test_registry_hands_out_the_guard_and_never_double_wraps() -> None:
    adapter = ScriptedAdapter()
    registry = AdapterRegistry(policies(enabled=True))
    guard = registry.register(adapter)
    assert registry.adapter("guard-probe") is guard and registry.owner_for(PREFIX + "x.v1") is guard
    assert unwrap(guard) is adapter
    typed_guard = cast(Any, guard)
    # fixture-style state stays reachable through the guard
    assert typed_guard.calls is adapter.calls
    typed_guard.answer = "scripted"
    assert adapter.answer == "scripted"
    with pytest.raises(AttributeError):
        typed_guard.inner = ScriptedAdapter()
    again = AdapterRegistry(policies(enabled=True)).register(guard)
    assert unwrap(again) is adapter


# ----------------------------------------------------------------------------
# effect gate, ownership and binding: the provider is never invoked
# ----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_external_effect_adapter_is_not_invoked_while_capability_is_off() -> None:
    adapter = ScriptedAdapter(answer=AdapterResult(Outcome.ACCEPTED, provider_operation_id="p-1"))
    guard = guarded(adapter, enabled=False)
    command = envelope()
    before = refusals("capability_disabled")
    result = await guard.execute(command, context(command))
    assert result.outcome is Outcome.REJECTED and result.error_class is ErrorClass.NON_RETRYABLE
    assert result.safe_error_code == "capability_disabled"
    assert adapter.calls == []
    assert refusals("capability_disabled") == before + 1


@pytest.mark.asyncio
async def test_no_effect_adapter_runs_with_capability_off() -> None:
    adapter = ScriptedAdapter(external_effect=False, answer=AdapterResult(Outcome.ACCEPTED, provider_operation_id="p-1"))
    guard = guarded(adapter, enabled=False)
    command = envelope()
    assert (await guard.execute(command, context(command))).outcome is Outcome.ACCEPTED
    assert adapter.calls == ["execute"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "updates,ctx_updates,reason",
    [
        ({"command_type": "other.write.v1", "target": "other-target", "capability": "OTHER_WRITE"}, {}, "adapter_not_owner"),
        ({"command_type": "unrouted.write.v1"}, {}, "adapter_not_owner"),
        ({"capability": "OTHER_WRITE"}, {}, "envelope_policy_mismatch"),
        ({"target": "other-target"}, {}, "envelope_policy_mismatch"),
        ({}, {"tenant_id": "tenant-b"}, "context_binding_mismatch"),
        ({}, {"command_id": str(uuid4())}, "context_binding_mismatch"),
    ],
)
async def test_guard_refuses_commands_this_adapter_must_not_execute(updates: dict[str, Any], ctx_updates: dict[str, Any], reason: str) -> None:
    adapter = ScriptedAdapter(answer=AdapterResult(Outcome.ACCEPTED, provider_operation_id="p-1"))
    guard = guarded(adapter)
    command = envelope(**updates)
    result = await guard.execute(command, context(command, **ctx_updates))
    assert result.outcome is Outcome.REJECTED and result.safe_error_code == reason
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_cross_tenant_readback_reconcile_status_and_cancel_never_reach_the_adapter() -> None:
    adapter = ScriptedAdapter(readback_answer=ReadbackResult(ReadbackStatus.MATCHED), cancel_answer=AdapterResult(Outcome.CANCELLED))
    guard = guarded(adapter)
    command = envelope()
    foreign = context(command, tenant_id="tenant-b")
    op = operation(command)
    assert (await guard.readback(op, foreign)).status is ReadbackStatus.UNAVAILABLE
    assert (await guard.reconcile(op, foreign)).status is ReadbackStatus.UNAVAILABLE
    assert (await guard.status(op, foreign)).outcome is Outcome.UNKNOWN
    assert (await guard.cancel(op, foreign)).outcome is Outcome.UNKNOWN
    assert adapter.calls == []


# ----------------------------------------------------------------------------
# result normalisation
# ----------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer,expected_outcome,expected_class,expected_code",
    [
        (None, Outcome.UNKNOWN, ErrorClass.AMBIGUOUS, "unnormalizable_result"),
        ({"status": "ok"}, Outcome.UNKNOWN, ErrorClass.AMBIGUOUS, "unnormalizable_result"),
        (AdapterResult("SHIPPED"), Outcome.UNKNOWN, ErrorClass.AMBIGUOUS, "nonconforming_outcome"),  # type: ignore[arg-type]
        # an acknowledgement carrying an error contradicts itself: reconcile
        (AdapterResult(Outcome.ACCEPTED, error_class=ErrorClass.NON_RETRYABLE, safe_error_code="x"), Outcome.UNKNOWN, ErrorClass.AMBIGUOUS, "x"),
        # "retry" while saying the effect may exist must never be resent
        (AdapterResult(Outcome.TRANSIENT, error_class=ErrorClass.AMBIGUOUS), Outcome.UNKNOWN, ErrorClass.AMBIGUOUS, "contradictory_result"),
        (AdapterResult(Outcome.REJECTED, error_class=ErrorClass.AMBIGUOUS), Outcome.UNKNOWN, ErrorClass.AMBIGUOUS, "contradictory_result"),
        # missing classes get the outcome's deterministic default
        (AdapterResult(Outcome.REJECTED), Outcome.REJECTED, ErrorClass.NON_RETRYABLE, "rejected_unspecified"),
        (AdapterResult(Outcome.TRANSIENT), Outcome.TRANSIENT, ErrorClass.RETRYABLE_BEFORE_EFFECT, "transient_unspecified"),
        (AdapterResult(Outcome.UNKNOWN, error_class=ErrorClass.NON_RETRYABLE), Outcome.UNKNOWN, ErrorClass.AMBIGUOUS, "unknown_unspecified"),
        (AdapterResult(Outcome.UNSUPPORTED), Outcome.UNSUPPORTED, ErrorClass.UNSUPPORTED, "unsupported_unspecified"),
        (AdapterResult(Outcome.REJECTED, error_class=ErrorClass.PROVIDER_AUTH, safe_error_code="ProviderAuthError"), Outcome.REJECTED, ErrorClass.PROVIDER_AUTH, "provider_auth_error"),
        (AdapterResult(Outcome.TRANSIENT, error_class=ErrorClass.PROVIDER_RATE_LIMITED), Outcome.TRANSIENT, ErrorClass.PROVIDER_RATE_LIMITED, "transient_unspecified"),
        (AdapterResult(Outcome.ACCEPTED, provider_operation_id="msg-1"), Outcome.ACCEPTED, None, None),
    ],
)
async def test_execute_results_are_coerced_into_the_contract(answer: Any, expected_outcome: Outcome, expected_class: ErrorClass | None, expected_code: str | None) -> None:
    adapter = ScriptedAdapter(answer=answer)
    guard = guarded(adapter)
    command = envelope()
    result = await guard.execute(command, context(command))
    assert result.outcome is expected_outcome
    assert result.error_class is expected_class
    assert result.safe_error_code == expected_code


def test_contradictions_and_malformed_results_are_counted() -> None:
    before = violations("execute_contradiction")
    conform_result(AdapterResult(Outcome.COMPLETED, error_class=ErrorClass.AMBIGUOUS), adapter_id="guard-probe", operation="execute")
    assert violations("execute_contradiction") == before + 1
    before = violations("execute_result_type")
    conform_result(object(), adapter_id="guard-probe", operation="execute")
    assert violations("execute_result_type") == before + 1


def test_error_codes_are_deterministic_bounded_slugs() -> None:
    assert safe_error_code("ReadTimeout", default="d") == "read_timeout"
    assert safe_error_code("CrmBridgeUnavailable", default="d") == "crm_bridge_unavailable"
    assert safe_error_code("provider_http_503", default="d") == "provider_http_503"
    leaked = safe_error_code("refused for jane.doe@example.com (+1 555 0100) Bearer abc", default="d")
    assert "@" not in leaked and " " not in leaked and "+" not in leaked
    assert len(safe_error_code("x" * 500, default="d")) == 64
    assert safe_error_code("   ", default="fallback") == "fallback"
    assert safe_error_code(None, default="fallback") == "fallback"
    assert safe_error_code("ReadTimeout", default="d") == safe_error_code("ReadTimeout", default="d")


def test_provider_handles_and_details_are_bounded_and_redacted() -> None:
    assert safe_operation_id("msg-1") == "msg-1"
    assert safe_operation_id(42) == "42"
    for bad in ("", "has space", "x" * 257, "line\nbreak", {"id": 1}, True, None):
        assert safe_operation_id(bad) is None
    details = safe_mapping(
        {
            "api_key": "k",
            "nested": {"client_secret": "s"},
            "drift": ["body", "summary"],
            "objects": [{"a": 1}],
            "count": 2,
            "long": "y" * 1000,
            **{f"k{index}": index for index in range(40)},
        }
    )
    assert details["api_key"] == "[REDACTED]"
    assert "nested" not in details and "objects" not in details  # non-scalar values are dropped
    assert details["drift"] == ["body", "summary"] and details["count"] == 2
    assert len(details["long"]) == 256
    assert len(details) <= 32
    assert safe_mapping("not a mapping") == {}


@pytest.mark.asyncio
async def test_malformed_provider_handle_is_dropped_not_persisted() -> None:
    adapter = ScriptedAdapter(answer=AdapterResult(Outcome.ACCEPTED, provider_operation_id="has a space\n"))
    guard = guarded(adapter)
    command = envelope()
    result = await guard.execute(command, context(command))
    assert result.outcome is Outcome.ACCEPTED and result.provider_operation_id is None


# ----------------------------------------------------------------------------
# readback / cancel / readiness / classification
# ----------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer,expected",
    [
        (None, ReadbackStatus.UNAVAILABLE),
        (AdapterResult(Outcome.COMPLETED), ReadbackStatus.UNAVAILABLE),
        (ReadbackResult("FOUND"), ReadbackStatus.UNAVAILABLE),  # type: ignore[arg-type]
        (ReadbackResult(ReadbackStatus.MATCHED, provider_operation_id="p-1", evidence={"token": "t", "state": "sent"}), ReadbackStatus.MATCHED),
        (ReadbackResult(ReadbackStatus.NOT_FOUND), ReadbackStatus.NOT_FOUND),
    ],
)
async def test_readback_answers_are_coerced_and_malformed_ones_stay_parked(answer: Any, expected: ReadbackStatus) -> None:
    adapter = ScriptedAdapter(readback_answer=answer)
    guard = guarded(adapter)
    command = envelope()
    readback = await guard.readback(operation(command), context(command))
    assert readback.status is expected
    if expected is ReadbackStatus.MATCHED:
        assert readback.evidence == {"token": "[REDACTED]", "state": "sent"} and readback.safe_error_code is None
    else:
        assert readback.safe_error_code
    reconciled = await guard.reconcile(operation(command), context(command))
    assert reconciled.status is expected


def test_readback_codes_default_per_status() -> None:
    assert conform_readback(ReadbackResult(ReadbackStatus.MISMATCH), adapter_id="a", operation="readback").safe_error_code == "readback_mismatch"
    matched = conform_readback(ReadbackResult(ReadbackStatus.MATCHED), adapter_id="a", operation="readback")
    assert matched.safe_error_code is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer,expected",
    [
        (AdapterResult(Outcome.CANCELLED), Outcome.CANCELLED),
        (AdapterResult(Outcome.UNSUPPORTED, error_class=ErrorClass.UNSUPPORTED), Outcome.UNSUPPORTED),
        (AdapterResult(Outcome.ACCEPTED), Outcome.UNKNOWN),
        (AdapterResult(Outcome.TRANSIENT, error_class=ErrorClass.RETRYABLE_BEFORE_EFFECT), Outcome.UNKNOWN),
        ("cancelled", Outcome.UNKNOWN),
    ],
)
async def test_cancel_never_claims_success_it_cannot_prove(answer: Any, expected: Outcome) -> None:
    adapter = ScriptedAdapter(cancel_answer=answer)
    guard = guarded(adapter)
    command = envelope()
    assert (await guard.cancel(operation(command), context(command))).outcome is expected


@dataclass
class BrokenAdapter(ScriptedAdapter):
    readiness_answer: Any = None

    async def readiness(self, context: AdapterContext) -> AdapterReadiness:
        if isinstance(self.readiness_answer, Exception):
            raise self.readiness_answer
        return self.readiness_answer

    def normalize_result(self, raw: Any) -> AdapterResult:
        raise KeyError("normaliser bug")

    def classify_error(self, error: BaseException) -> ErrorClass:
        return "RETRY_PLEASE"  # type: ignore[return-value]


@pytest.mark.asyncio
async def test_readiness_normaliser_and_classifier_failures_fail_closed() -> None:
    adapter = BrokenAdapter(answer=AdapterResult(Outcome.ACCEPTED))
    guard = guarded(adapter)
    command = envelope()
    adapter.readiness_answer = RuntimeError("secret detail https://user:pw@host")
    readiness = await guard.readiness(context(command))
    assert readiness.ready is False and readiness.detail == "runtime_error"
    adapter.readiness_answer = {"ready": True}
    assert (await guard.readiness(context(command))).ready is False
    adapter.readiness_answer = AdapterReadiness(ready=1, detail="z" * 500)  # type: ignore[arg-type]
    shaped = await guard.readiness(context(command))
    assert shaped.ready is False and len(shaped.detail) == 160
    result = await guard.execute(command, context(command))
    assert result.outcome is Outcome.UNKNOWN and result.safe_error_code == "key_error"
    assert guard.classify_error(RuntimeError("x")) is ErrorClass.AMBIGUOUS


@pytest.mark.asyncio
async def test_execute_exceptions_propagate_for_the_bus_to_classify() -> None:
    @dataclass
    class Raising(ScriptedAdapter):
        async def execute(self, command: CommandEnvelope, context: AdapterContext) -> AdapterResult:
            raise TimeoutError("stalled after send")

    guard = guarded(Raising())
    command = envelope()
    with pytest.raises(TimeoutError):
        await guard.execute(command, context(command))
    assert guard.classify_error(TimeoutError()) is ErrorClass.AMBIGUOUS
