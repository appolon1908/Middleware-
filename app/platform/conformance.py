"""Runtime enforcement of the adapter contract.

The conformance suite proves an adapter *can* behave; this module makes sure
a registered adapter *does*, on every call, whatever it returns. The
:class:`~app.platform.registry.AdapterRegistry` wraps every adapter it accepts
in a :class:`ConformingAdapter`, so the kernel, the execution bus and the
reconciler only ever see normalised answers:

* **ownership and binding** — ``execute`` refuses a command whose type is not
  routed to this adapter by the command registry, whose envelope names another
  target or capability than the owning policy, or whose context belongs to
  another tenant or command. Nothing reaches the provider in those cases.
* **effect gate** — an adapter that declares ``external_effect`` is never
  invoked while the capability registry (``config/capabilities.v2.json``)
  leaves the owning capability off. This is defence in depth beneath the
  Safety Gate: even a caller that skipped the gate cannot cause an effect.
* **normalised results** — outcome, error class, error code, provider handle
  and details are coerced into the closed contract. Contradictions fail
  closed: an acknowledgement that carries an error, or a "retryable" answer
  whose class says the effect may exist, becomes ``UNKNOWN`` (reconcile,
  never resend). Error codes are deterministic slugs; details are bounded,
  scalar and redacted.
* **readback** — anything but a :class:`ReadbackResult` is ``UNAVAILABLE``
  (the operation stays parked, it is never completed or re-executed on a
  malformed answer).

Every violation is counted (``codestra_adapter_contract_violations_total``)
and logged without payload data.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any

from prometheus_client import Counter

from app.commands import CommandEnvelope, CommandOperation, CommandPolicyRegistry, redact_metadata
from app.platform.adapter import (
    Adapter,
    AdapterCapabilities,
    AdapterContext,
    AdapterReadiness,
    AdapterResult,
    ErrorClass,
    Outcome,
    ReadbackResult,
    ReadbackStatus,
)

logger = logging.getLogger("codestra.platform.conformance")

CONTRACT_VIOLATIONS = Counter(
    "codestra_adapter_contract_violations_total",
    "Adapter answers coerced into the adapter contract",
    ["adapter", "kind"],
)
GUARD_REFUSALS = Counter(
    "codestra_adapter_guard_refusals_total",
    "Adapter executions refused before the provider was invoked",
    ["adapter", "reason"],
)

MAX_ERROR_CODE_LENGTH = 64
MAX_OPERATION_ID_LENGTH = 256
MAX_DETAIL_ENTRIES = 32
MAX_DETAIL_KEY_LENGTH = 64
MAX_DETAIL_STRING_LENGTH = 256
MAX_DETAIL_LIST_ITEMS = 16
MAX_READINESS_DETAIL_LENGTH = 160

_ACK = frozenset({Outcome.ACCEPTED, Outcome.COMPLETED})
# The error classes each outcome may carry; the first is the default.
_ALLOWED_CLASSES: Mapping[Outcome, tuple[ErrorClass | None, ...]] = {
    Outcome.ACCEPTED: (None,),
    Outcome.COMPLETED: (None,),
    Outcome.REJECTED: (ErrorClass.NON_RETRYABLE, ErrorClass.PROVIDER_AUTH, ErrorClass.UNSUPPORTED),
    Outcome.TRANSIENT: (ErrorClass.RETRYABLE_BEFORE_EFFECT, ErrorClass.PROVIDER_RATE_LIMITED),
    Outcome.UNKNOWN: (ErrorClass.AMBIGUOUS,),
    Outcome.CANCELLED: (None,),
    Outcome.UNSUPPORTED: (ErrorClass.UNSUPPORTED,),
}
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_NOT_SLUG = re.compile(r"[^a-z0-9_.:-]+")
_OPERATION_ID = re.compile(r"^[\x21-\x7e]+$")


def _violation(adapter_id: str, kind: str) -> None:
    CONTRACT_VIOLATIONS.labels(adapter=adapter_id, kind=kind).inc()
    logger.warning("adapter_contract_violation", extra={"adapter": adapter_id, "violation": kind})


def safe_error_code(value: object, *, default: str) -> str:
    """A deterministic, bounded, lowercase slug (``ReadTimeout`` → ``read_timeout``).

    Only identifier-like characters survive, so free text, addresses or
    numbers glued to punctuation cannot be smuggled through an error code.
    """
    if not isinstance(value, str) or not value.strip():
        return default
    text = _CAMEL_BOUNDARY.sub("_", value.strip()).lower()
    text = _NOT_SLUG.sub("_", text).strip("_.:-")
    return text[:MAX_ERROR_CODE_LENGTH].rstrip("_.:-") or default


def safe_operation_id(value: object) -> str | None:
    """An opaque provider handle: printable ASCII without spaces, bounded."""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    text = str(value)
    if not text or len(text) > MAX_OPERATION_ID_LENGTH or not _OPERATION_ID.match(text):
        return None
    return text


def _scalar(value: object) -> str | int | float | bool | None | object:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:MAX_DETAIL_STRING_LENGTH]
    return _DROP


_DROP = object()


def safe_mapping(value: object) -> dict[str, Any]:
    """Bounded, redacted, JSON-safe details: scalar values and short scalar lists."""
    if not isinstance(value, Mapping):
        return {}
    cleaned: dict[str, Any] = {}
    for raw_key, raw_value in redact_metadata(dict(value)).items():
        if len(cleaned) >= MAX_DETAIL_ENTRIES:
            break
        key = str(raw_key)[:MAX_DETAIL_KEY_LENGTH]
        if isinstance(raw_value, (list, tuple)):
            items = [_scalar(item) for item in raw_value[:MAX_DETAIL_LIST_ITEMS]]
            if all(item is not _DROP for item in items):
                cleaned[key] = items
            continue
        scalar = _scalar(raw_value)
        if scalar is not _DROP:
            cleaned[key] = scalar
    return cleaned


def conform_result(raw: object, *, adapter_id: str, operation: str) -> AdapterResult:
    """Coerce an adapter answer into the closed result contract (fail closed)."""
    if not isinstance(raw, AdapterResult):
        _violation(adapter_id, f"{operation}_result_type")
        return AdapterResult(Outcome.UNKNOWN, error_class=ErrorClass.AMBIGUOUS, safe_error_code="unnormalizable_result")
    try:
        outcome = Outcome(raw.outcome)
    except ValueError:
        _violation(adapter_id, f"{operation}_outcome")
        return AdapterResult(Outcome.UNKNOWN, error_class=ErrorClass.AMBIGUOUS, safe_error_code="nonconforming_outcome")
    try:
        error_class: ErrorClass | None = None if raw.error_class is None else ErrorClass(raw.error_class)
    except ValueError:
        error_class = ErrorClass.AMBIGUOUS
        _violation(adapter_id, f"{operation}_error_class")

    allowed = _ALLOWED_CLASSES[outcome]
    if error_class not in allowed:
        if outcome in _ACK or (outcome is Outcome.TRANSIENT and error_class is ErrorClass.AMBIGUOUS) or (
            outcome is Outcome.REJECTED and error_class is ErrorClass.AMBIGUOUS
        ):
            # The adapter contradicts itself about whether an effect may exist:
            # an acknowledgement with an error, or a retry/refusal whose class
            # says the request may have been applied. Reconcile; never resend.
            _violation(adapter_id, f"{operation}_contradiction")
            return AdapterResult(
                Outcome.UNKNOWN,
                provider_operation_id=safe_operation_id(raw.provider_operation_id),
                error_class=ErrorClass.AMBIGUOUS,
                safe_error_code=safe_error_code(raw.safe_error_code, default="contradictory_result"),
                safe_details=safe_mapping(raw.safe_details),
            )
        if error_class is not None:
            _violation(adapter_id, f"{operation}_error_class")
        error_class = allowed[0]

    code: str | None
    if outcome in _ACK or outcome is Outcome.CANCELLED:
        code = safe_error_code(raw.safe_error_code, default="") or None
    else:
        code = safe_error_code(raw.safe_error_code, default=f"{outcome.value.lower()}_unspecified")
    operation_id = safe_operation_id(raw.provider_operation_id)
    if raw.provider_operation_id is not None and operation_id is None:
        _violation(adapter_id, f"{operation}_operation_id")
    return AdapterResult(
        outcome,
        provider_operation_id=operation_id,
        error_class=error_class,
        safe_error_code=code,
        safe_details=safe_mapping(raw.safe_details),
    )


def conform_readback(raw: object, *, adapter_id: str, operation: str) -> ReadbackResult:
    """Coerce a readback into the contract; a malformed answer is ``UNAVAILABLE``."""
    if not isinstance(raw, ReadbackResult):
        _violation(adapter_id, f"{operation}_result_type")
        return ReadbackResult(ReadbackStatus.UNAVAILABLE, safe_error_code="unnormalizable_readback")
    try:
        status = ReadbackStatus(raw.status)
    except ValueError:
        _violation(adapter_id, f"{operation}_status")
        return ReadbackResult(ReadbackStatus.UNAVAILABLE, safe_error_code="nonconforming_readback_status")
    operation_id = safe_operation_id(raw.provider_operation_id)
    if raw.provider_operation_id is not None and operation_id is None:
        _violation(adapter_id, f"{operation}_operation_id")
    code = None if status is ReadbackStatus.MATCHED and raw.safe_error_code is None else safe_error_code(
        raw.safe_error_code, default=f"readback_{status.value.lower()}"
    )
    return ReadbackResult(status, provider_operation_id=operation_id, evidence=safe_mapping(raw.evidence), safe_error_code=code)


def _refused(adapter_id: str, reason: str, *, error_class: ErrorClass = ErrorClass.NON_RETRYABLE) -> AdapterResult:
    GUARD_REFUSALS.labels(adapter=adapter_id, reason=reason).inc()
    logger.warning("adapter_execution_refused", extra={"adapter": adapter_id, "reason": reason})
    return AdapterResult(Outcome.REJECTED, error_class=error_class, safe_error_code=reason)


class ConformingAdapter:
    """The registry's view of an adapter: same surface, enforced contract.

    Attribute reads that are not part of the contract (fixture state, legacy
    handles) fall through to the wrapped adapter, so callers that inspect the
    concrete adapter keep working.
    """

    def __init__(self, inner: Adapter, *, policies: CommandPolicyRegistry, advertised: AdapterCapabilities) -> None:
        object.__setattr__(self, "inner", inner)
        object.__setattr__(self, "policies", policies)
        object.__setattr__(self, "advertised", advertised)
        object.__setattr__(self, "adapter_id", inner.adapter_id)

    def __getattr__(self, name: str) -> Any:
        return getattr(object.__getattribute__(self, "inner"), name)

    def __setattr__(self, name: str, value: Any) -> None:
        # Writes target the wrapped adapter (fixtures are scripted in tests);
        # the guard's own bindings are immutable.
        if name in {"inner", "policies", "advertised", "adapter_id"}:
            raise AttributeError(f"{name} is fixed for the lifetime of the registration")
        setattr(self.inner, name, value)

    def __repr__(self) -> str:
        return f"ConformingAdapter({self.inner!r})"

    # --- configuration / description -------------------------------------------------
    def validate_config(self) -> None:
        self.inner.validate_config()

    def capabilities(self) -> AdapterCapabilities:
        return self.advertised

    async def readiness(self, context: AdapterContext) -> AdapterReadiness:
        try:
            raw = await self.inner.readiness(context)
        except Exception as exc:  # noqa: BLE001 - readiness never raises into a probe
            return AdapterReadiness(ready=False, detail=safe_error_code(type(exc).__name__, default="readiness_failed"))
        if not isinstance(raw, AdapterReadiness):
            _violation(self.adapter_id, "readiness_result_type")
            return AdapterReadiness(ready=False, detail="unnormalizable_readiness")
        detail = raw.detail if isinstance(raw.detail, str) else ""
        return AdapterReadiness(ready=raw.ready is True, detail=detail[:MAX_READINESS_DETAIL_LENGTH])

    # --- effects ----------------------------------------------------------------------
    def refusal(self, command: CommandEnvelope, context: AdapterContext) -> str | None:
        """Why this adapter must not be invoked for ``command`` (``None``: it may)."""
        policy = self.policies.resolve(command.command_type)
        if policy is None or policy.target not in self.advertised.connector_ids:
            return "adapter_not_owner"
        if command.target != policy.target or command.capability != policy.capability:
            return "envelope_policy_mismatch"
        if policy.capability not in self.advertised.capabilities:
            return "adapter_not_owner"
        if context.tenant_id != command.tenant_id or context.command_id != str(command.command_id):
            return "context_binding_mismatch"
        if self.advertised.external_effect and self.policies.capabilities.get(policy.capability) is not True:
            return "capability_disabled"
        return None

    async def execute(self, command: CommandEnvelope, context: AdapterContext) -> AdapterResult:
        reason = self.refusal(command, context)
        if reason is not None:
            return _refused(self.adapter_id, reason)
        # Exceptions propagate unchanged: the bus classifies them through
        # ``classify_error`` and owns timeout/cancellation semantics.
        raw = await self.inner.execute(command, context)
        return self.normalize_result(raw)

    async def status(self, operation: CommandOperation, context: AdapterContext) -> AdapterResult:
        if context.tenant_id != operation.tenant_id:
            return AdapterResult(Outcome.UNKNOWN, error_class=ErrorClass.AMBIGUOUS, safe_error_code="context_binding_mismatch")
        return conform_result(await self.inner.status(operation, context), adapter_id=self.adapter_id, operation="status")

    async def readback(self, operation: CommandOperation, context: AdapterContext) -> ReadbackResult:
        if context.tenant_id != operation.tenant_id:
            return ReadbackResult(ReadbackStatus.UNAVAILABLE, safe_error_code="context_binding_mismatch")
        return conform_readback(await self.inner.readback(operation, context), adapter_id=self.adapter_id, operation="readback")

    async def reconcile(self, operation: CommandOperation, context: AdapterContext) -> ReadbackResult:
        if context.tenant_id != operation.tenant_id:
            return ReadbackResult(ReadbackStatus.UNAVAILABLE, safe_error_code="context_binding_mismatch")
        return conform_readback(await self.inner.reconcile(operation, context), adapter_id=self.adapter_id, operation="reconcile")

    async def cancel(self, operation: CommandOperation, context: AdapterContext) -> AdapterResult:
        if context.tenant_id != operation.tenant_id:
            return AdapterResult(Outcome.UNKNOWN, error_class=ErrorClass.AMBIGUOUS, safe_error_code="context_binding_mismatch")
        result = conform_result(await self.inner.cancel(operation, context), adapter_id=self.adapter_id, operation="cancel")
        if result.outcome in _ACK or result.outcome is Outcome.TRANSIENT:
            # A cancel cannot "accept" or be blindly retried: whether the effect
            # was stopped is only known from a readback.
            _violation(self.adapter_id, "cancel_outcome")
            return AdapterResult(Outcome.UNKNOWN, error_class=ErrorClass.AMBIGUOUS, safe_error_code="cancel_outcome_unknown")
        return result

    # --- normalisation ------------------------------------------------------------------
    def normalize_result(self, raw: Any) -> AdapterResult:
        try:
            normalized = self.inner.normalize_result(raw)
        except Exception as exc:  # noqa: BLE001 - a normaliser crash is an unknown outcome
            _violation(self.adapter_id, "normalize_raised")
            return AdapterResult(Outcome.UNKNOWN, error_class=ErrorClass.AMBIGUOUS, safe_error_code=safe_error_code(type(exc).__name__, default="normalize_failed"))
        return conform_result(normalized, adapter_id=self.adapter_id, operation="execute")

    def classify_error(self, error: BaseException) -> ErrorClass:
        try:
            klass = self.inner.classify_error(error)
            return ErrorClass(klass)
        except Exception:  # noqa: BLE001 - an unclassifiable error is ambiguous
            _violation(self.adapter_id, "classify_error")
            return ErrorClass.AMBIGUOUS

    def redact(self, value: Any) -> Any:
        return self.inner.redact(value)


def unwrap(adapter: object) -> object:
    """The concrete adapter behind a registry guard (for diagnostics and tests)."""
    return adapter.inner if isinstance(adapter, ConformingAdapter) else adapter


__all__ = [
    "CONTRACT_VIOLATIONS",
    "GUARD_REFUSALS",
    "ConformingAdapter",
    "conform_readback",
    "conform_result",
    "safe_error_code",
    "safe_mapping",
    "safe_operation_id",
    "unwrap",
]
