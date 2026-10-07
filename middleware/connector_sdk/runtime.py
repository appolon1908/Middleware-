"""Connector command runtime with capability and read-back enforcement."""

from __future__ import annotations

import json
import re
import random
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Any

from .errors import (
    CapabilityDisabledError,
    CommandNotAllowedError,
    ConnectorStateError,
    ReadBackRequiredError,
    StandardsValidationError,
)
from .interfaces import CapabilityProvider, ConnectorAdapter
from .models import (
    CommandOutcome,
    CommandRequest,
    CommandResult,
    CommandPolicy,
    ConnectorState,
    ConnectionTestResult,
    ConnectorHealth,
)
from .operations import InMemoryOperationStore, OperationStore, request_fingerprint
from .registry import ConnectorRegistry
from .standards import (
    SECRET_KEY_NAMES,
    deep_thaw,
    forbidden_paths,
    validate_traceparent,
    validate_tracestate,
)

_SAFE_REFERENCE = re.compile(r"^[A-Za-z0-9._:/-]{1,256}$")


def _required_uuid(value: str, label: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except (ValueError, TypeError, AttributeError) as error:
        raise CommandNotAllowedError(f"{label} must be a UUID") from error
    return str(parsed)


def _validate_result(
    result: CommandResult,
    *,
    prior_operation_id: str | None = None,
    prior_provider_reference: str | None = None,
) -> CommandResult:
    if not isinstance(result, CommandResult):
        raise CommandNotAllowedError("adapter returned an invalid result type")
    if not isinstance(result.outcome, CommandOutcome):
        raise CommandNotAllowedError("adapter result outcome is invalid")
    if type(result.retryable) is not bool or (
        result.retryable and result.outcome is not CommandOutcome.FAILED
    ):
        raise CommandNotAllowedError("only definitive failed results can be retryable")
    _safe_mapping(result.safe_result, "adapter safe_result")
    if (
        not isinstance(result.operation_id, str)
        or _SAFE_REFERENCE.fullmatch(result.operation_id) is None
    ):
        raise CommandNotAllowedError("adapter result operation_id is invalid")
    if prior_operation_id is not None and result.operation_id != prior_operation_id:
        raise CommandNotAllowedError(
            "adapter changed operation_id across one command lifecycle"
        )
    if prior_provider_reference is not None and result.provider_reference not in (
        None,
        prior_provider_reference,
    ):
        raise CommandNotAllowedError(
            "adapter changed provider reference during settlement"
        )
    secret_paths = forbidden_paths(result.safe_result, SECRET_KEY_NAMES)
    if secret_paths:
        raise CommandNotAllowedError(
            "adapter safe_result contains forbidden secret fields: "
            + ", ".join(secret_paths)
        )
    if result.provider_reference is not None and (
        not isinstance(result.provider_reference, str)
        or _SAFE_REFERENCE.fullmatch(result.provider_reference) is None
    ):
        raise CommandNotAllowedError("provider_reference is invalid")
    if result.error_code is not None and (
        not isinstance(result.error_code, str)
        or not re.fullmatch(
            r"^[A-Z][A-Z0-9_]{1,127}$",
            result.error_code,
        )
    ):
        raise CommandNotAllowedError("adapter error_code is invalid")
    if prior_provider_reference is not None and result.provider_reference is None:
        return replace(result, provider_reference=prior_provider_reference)
    return result


def _safe_mapping(value: Any, label: str) -> None:
    if not isinstance(value, Mapping):
        raise CommandNotAllowedError(f"{label} must be an object")
    try:
        encoded = json.dumps(deep_thaw(value), allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError, RecursionError) as error:
        raise CommandNotAllowedError(
            f"{label} must contain finite JSON values"
        ) from error
    if len(encoded.encode("utf-8")) > 1_048_576:
        raise CommandNotAllowedError(f"{label} exceeds the size limit")


class StaticCapabilityProvider:
    """Small test/development provider. Production uses Middleware persistence."""

    def __init__(self, values: Mapping[tuple[str, str], bool]) -> None:
        self._values = dict(values)

    def is_enabled(self, tenant_id: str, capability: str) -> bool:
        return bool(self._values.get((tenant_id, capability), False))


class ConnectorRuntime:
    def __init__(
        self,
        registry: ConnectorRegistry,
        capabilities: CapabilityProvider,
        *,
        operations: OperationStore | None = None,
        authorize: Callable[[CommandRequest], bool] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        observe: Callable[[str, Mapping[str, Any]], None] | None = None,
    ) -> None:
        self._registry = registry
        self._capabilities = capabilities
        self._operations = (
            operations if operations is not None else InMemoryOperationStore()
        )
        self._sleep = sleep
        self._observe = observe
        self._authorize = authorize

    def execute(self, request: CommandRequest) -> CommandResult:
        if self._authorize is not None and self._authorize(request) is not True:
            raise CapabilityDisabledError("command authorization denied")
        if not request.context.tenant_id:
            raise CommandNotAllowedError("tenant_id is required")
        _required_uuid(request.context.tenant_id, "tenant_id")
        _required_uuid(request.command_id, "command_id")
        _required_uuid(request.context.correlation_id, "correlation_id")
        if (
            not isinstance(request.context.actor_id, str)
            or not 1 <= len(request.context.actor_id) <= 300
        ):
            raise CommandNotAllowedError("actor_id is invalid")
        if (
            not isinstance(request.context.causation_id, str)
            or not 1 <= len(request.context.causation_id) <= 180
        ):
            raise CommandNotAllowedError("causation_id is invalid")
        key = request.context.idempotency_key
        if (
            not isinstance(key, str)
            or not 8 <= len(key) <= 180
            or any(ord(char) < 33 or ord(char) > 126 for char in key)
        ):
            raise CommandNotAllowedError(
                "idempotency_key must contain 8 to 180 printable non-space characters"
            )
        _safe_mapping(request.payload, "payload")
        if type(request.command_version) is not int or request.command_version < 1:
            raise CommandNotAllowedError("command_version must be positive")
        if (
            not isinstance(request.command_type, str)
            or re.fullmatch(
                r"[a-z][a-z0-9]*(?:\.[a-z][a-z0-9_]*)+\.v[1-9][0-9]*",
                request.command_type,
            )
            is None
            or not request.command_type.endswith(f".v{request.command_version}")
        ):
            raise CommandNotAllowedError(
                "command_type must include the matching command_version"
            )
        try:
            validate_traceparent(request.context.traceparent)
            validate_tracestate(request.context.tracestate)
        except StandardsValidationError as error:
            raise CommandNotAllowedError(str(error)) from error

        record = self._registry.get(request.connector_id)
        forbidden = set(record.manifest.forbidden_payload_keys)
        forbidden.update(SECRET_KEY_NAMES)
        secret_paths = forbidden_paths(request.payload, forbidden)
        if secret_paths:
            raise CommandNotAllowedError(
                "payload contains forbidden secret fields: " + ", ".join(secret_paths)
            )
        if record.state is not ConnectorState.ACTIVE:
            raise ConnectorStateError(
                f"connector {request.connector_id} is {record.state.value}"
            )

        resolved, policy = self._registry.resolve_command(request.command_type)
        if resolved.manifest.connector_id != request.connector_id:
            raise CommandNotAllowedError(
                f"{request.command_type} is owned by "
                f"{resolved.manifest.connector_id}, "
                f"not {request.connector_id}"
            )

        for forbidden_prefix in record.manifest.forbidden_command_prefixes:
            if request.command_type.startswith(forbidden_prefix):
                raise CommandNotAllowedError(request.command_type)

        capability = policy.required_capability
        if capability != "NONE":
            snapshot_value = request.context.capability_snapshot.get(capability)
            authoritative_value = self._capabilities.is_enabled(
                request.context.tenant_id,
                capability,
            )
            if snapshot_value is not True or authoritative_value is not True:
                raise CapabilityDisabledError(capability)

        adapter = self._registry.adapter_factory(request.connector_id)(record.manifest)
        fingerprint = request_fingerprint(request, record.manifest_digest)
        with self._operations.acquire(request, fingerprint) as lease:
            snapshot = lease.snapshot
            result = snapshot.result
            if result is not None:
                result = _validate_result(result)
                if result.outcome in {
                    CommandOutcome.COMPLETED,
                    CommandOutcome.FAILED,
                    CommandOutcome.CANCELLED,
                }:
                    self._event("replayed", request, result, snapshot.attempts)
                    return result
            attempts = snapshot.attempts
            if result is None:
                # Persist uncertainty BEFORE entering adapter code. A crash must
                # resume through reconciliation rather than a second submission.
                while attempts < policy.retry_policy.maximum_attempts:
                    current = self._registry.get(request.connector_id)
                    if current != record:
                        raise ConnectorStateError(
                            "connector binding or state changed before submission"
                        )
                    if (
                        self._authorize is not None
                        and self._authorize(request) is not True
                    ):
                        raise CapabilityDisabledError("command authorization denied")
                    if (
                        capability != "NONE"
                        and self._capabilities.is_enabled(
                            request.context.tenant_id, capability
                        )
                        is not True
                    ):
                        raise CapabilityDisabledError(capability)
                    attempts += 1
                    pending = CommandResult(
                        outcome=CommandOutcome.UNKNOWN,
                        operation_id=request.command_id,
                        error_code="SUBMISSION_IN_PROGRESS",
                    )
                    lease.save(pending, attempts=attempts)
                    result = self._invoke(
                        adapter.execute_command, request, None, "ADAPTER_EXCEPTION"
                    )
                    result = _validate_result(result)
                    journal_result = result
                    if (
                        policy.readback_required
                        and result.outcome is CommandOutcome.COMPLETED
                    ):
                        journal_result = replace(
                            result, outcome=CommandOutcome.SUBMITTED
                        )
                    lease.save(journal_result, attempts=attempts)
                    self._event("submitted", request, result, attempts)
                    if (
                        result.outcome is not CommandOutcome.FAILED
                        or not result.retryable
                    ):
                        break
                    if attempts < policy.retry_policy.maximum_attempts:
                        retry = policy.retry_policy
                        delay = min(
                            retry.maximum_backoff_seconds,
                            retry.initial_backoff_seconds * 2 ** (attempts - 1),
                        )
                        delay = min(
                            retry.maximum_backoff_seconds,
                            delay
                            * random.SystemRandom().uniform(
                                1 - retry.jitter_ratio, 1 + retry.jitter_ratio
                            ),
                        )
                        self._sleep(delay)
                if result is None:
                    raise ConnectorStateError("connector retry budget is empty")
            result = self._settle(adapter, request, result, policy)
            lease.save(result, attempts=attempts)
            self._event("settled", request, result, attempts)
            return result

    @staticmethod
    def _invoke(
        method: Callable[..., CommandResult],
        request: CommandRequest,
        prior: CommandResult | None,
        error_code: str,
    ) -> CommandResult:
        try:
            return method(request) if prior is None else method(request, prior)
        except Exception:
            # Adapter exception messages may include credentials or provider
            # response bodies. Preserve only a stable, safe error category.
            return CommandResult(
                outcome=CommandOutcome.UNKNOWN,
                operation_id=prior.operation_id if prior else request.command_id,
                provider_reference=prior.provider_reference if prior else None,
                error_code=error_code,
            )

    def _settle(
        self,
        adapter: ConnectorAdapter,
        request: CommandRequest,
        result: CommandResult,
        policy: CommandPolicy,
    ) -> CommandResult:
        if result.outcome is CommandOutcome.UNKNOWN:
            result = _validate_result(
                self._invoke(
                    adapter.reconcile_unknown,
                    request,
                    result,
                    "RECONCILIATION_UNAVAILABLE",
                ),
                prior_operation_id=result.operation_id,
                prior_provider_reference=result.provider_reference,
            )
            if result.outcome is CommandOutcome.UNKNOWN:
                return result
        if policy.readback_required and result.outcome in {
            CommandOutcome.ACCEPTED,
            CommandOutcome.SUBMITTED,
            CommandOutcome.COMPLETED,
        }:
            result = _validate_result(
                self._invoke(
                    adapter.read_back, request, result, "READBACK_UNAVAILABLE"
                ),
                prior_operation_id=result.operation_id,
                prior_provider_reference=result.provider_reference,
            )
            if result.outcome in {CommandOutcome.ACCEPTED, CommandOutcome.SUBMITTED}:
                raise ReadBackRequiredError(
                    "authoritative read-back did not reach a terminal state"
                )
            if (
                result.outcome is CommandOutcome.UNKNOWN
                and result.error_code != "READBACK_UNAVAILABLE"
            ):
                raise ReadBackRequiredError(
                    "authoritative read-back did not reach a terminal state"
                )
        return result

    def _event(
        self, event: str, request: CommandRequest, result: CommandResult, attempts: int
    ) -> None:
        if self._observe is not None:
            try:
                self._observe(
                    event,
                    {
                        "connector_id": request.connector_id,
                        "command_id": request.command_id,
                        "correlation_id": request.context.correlation_id,
                        "outcome": result.outcome.value,
                        "error_code": result.error_code,
                        "attempts": attempts,
                    },
                )
            except Exception:
                # Telemetry must never change the externally observed outcome.
                return

    def test_connection(
        self,
        connector_id: str,
        configuration: Mapping[str, Any],
    ) -> ConnectionTestResult:
        record = self._registry.get(connector_id)
        _safe_mapping(configuration, "configuration")
        if forbidden_paths(
            configuration,
            SECRET_KEY_NAMES | frozenset(record.manifest.forbidden_payload_keys),
        ):
            raise CommandNotAllowedError(
                "configuration contains forbidden secret fields"
            )
        adapter = self._registry.adapter_factory(connector_id)(record.manifest)
        errors = adapter.validate_configuration(
            record.manifest,
            configuration,
        )
        if errors:
            return ConnectionTestResult(ok=False, code="CONFIGURATION_INVALID")
        result = adapter.test_connection(
            record.manifest,
            configuration,
        )
        if (
            not isinstance(result, ConnectionTestResult)
            or type(result.ok) is not bool
            or not isinstance(result.code, str)
            or re.fullmatch(r"[A-Z][A-Z0-9_]{1,127}", result.code) is None
        ):
            raise CommandNotAllowedError("connection test result is invalid")
        _safe_mapping(result.safe_details, "connection test details")
        secret_paths = forbidden_paths(
            result.safe_details,
            SECRET_KEY_NAMES,
        )
        if secret_paths:
            raise CommandNotAllowedError(
                "connection test leaked forbidden fields: " + ", ".join(secret_paths)
            )
        return result

    def health(self, connector_id: str) -> ConnectorHealth:
        record = self._registry.get(connector_id)
        adapter = self._registry.adapter_factory(connector_id)(record.manifest)
        result = adapter.health()
        if (
            not isinstance(result, ConnectorHealth)
            or not isinstance(result.status, str)
            or result.status not in {"HEALTHY", "DEGRADED", "UNHEALTHY", "UNKNOWN"}
            or type(result.checked_at_epoch) is not int
            or result.checked_at_epoch < 0
        ):
            raise CommandNotAllowedError("health result is invalid")
        _safe_mapping(result.safe_details, "health details")
        secret_paths = forbidden_paths(
            result.safe_details,
            SECRET_KEY_NAMES,
        )
        if secret_paths:
            raise CommandNotAllowedError(
                "health result leaked forbidden fields: " + ", ".join(secret_paths)
            )
        return result
