"""Standalone media-engine adapters for the Middleware V3 command kernel.

Blender, Natron and Kdenlive/MLT stay independently deployable services.  This
module only bridges durable Middleware commands to their private SaaS APIs; it
never exposes native renderer ports and never moves media bytes through
Middleware.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Mapping
from urllib.parse import quote, urlsplit

import httpx

from app.commands import CommandEnvelope, CommandOperation
from app.core.config import Settings
from app.platform.adapter import (
    AdapterConfigurationError,
    AdapterContext,
    AdapterReadiness,
    AdapterResult,
    BaseAdapter,
    ErrorClass,
    Outcome,
    ReadbackResult,
    ReadbackStatus,
)

logger = logging.getLogger("codestra.platform.adapters.media")

MEDIA_RENDER_CAPABILITY = "MEDIA_RENDER"

_BLENDER_COMMANDS = {
    "media.blender.render.frame.v1": "render_frame",
    "media.blender.render.animation.v1": "render_animation",
}
_NATRON_COMMANDS = {
    "media.natron.render.project.v1": "render_project",
    "media.natron.render.writer.v1": "render_writer",
}
_KDENLIVE_COMMANDS = {
    "media.kdenlive.render.timeline.v1": "render_timeline",
}

_PENDING_STATES = frozenset({"queued", "running", "cancel_requested"})
_SUCCESS_STATES = frozenset({"succeeded"})
_FAILURE_STATES = frozenset({"failed", "cancelled"})


def _validate_origin(value: str, *, label: str) -> str:
    """Require an origin only; plaintext is allowed solely on loopback."""
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError as exc:
        raise AdapterConfigurationError(f"{label}: invalid base URL") from exc
    if not parsed.scheme or not parsed.hostname:
        raise AdapterConfigurationError(f"{label}: base URL must be absolute")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise AdapterConfigurationError(
            f"{label}: base URL cannot contain userinfo, query or fragment"
        )
    if parsed.path not in {"", "/"}:
        raise AdapterConfigurationError(f"{label}: base URL must be an origin")
    host = parsed.hostname.lower()
    if parsed.scheme == "https":
        return value.rstrip("/")
    if parsed.scheme == "http" and host in {"127.0.0.1", "::1", "localhost"}:
        return value.rstrip("/")
    raise AdapterConfigurationError(
        f"{label}: non-loopback media services require HTTPS"
    )


@dataclass
class MediaSaaSAdapter(BaseAdapter):
    """Bridge one standalone media SaaS API into the command kernel."""

    adapter_id: str = ""
    provider_family: str = "media"
    connector_ids: tuple[str, ...] = ()
    served_capabilities: tuple[str, ...] = (MEDIA_RENDER_CAPABILITY,)
    command_kinds: Mapping[str, str] = field(default_factory=dict)
    base_url: str = ""
    service_token: str = ""
    http: httpx.AsyncClient | None = None
    version: str = "media-saas/1.0"
    supports_readback: bool = True
    supports_cancel: bool = True
    supports_status: bool = True
    safe_reexecution: bool = False
    external_effect: bool = True

    def validate_config(self) -> None:
        BaseAdapter.validate_config(self)
        if not self.command_kinds:
            raise AdapterConfigurationError(
                f"{self.adapter_id}: no command mappings configured"
            )
        self.base_url = _validate_origin(
            self.base_url.strip(), label=self.adapter_id
        )
        if not self.service_token.strip():
            raise AdapterConfigurationError(
                f"{self.adapter_id}: service token is not configured"
            )
        if self.http is None:
            raise AdapterConfigurationError(
                f"{self.adapter_id}: shared HTTP client is required"
            )

    def _headers(
        self,
        *,
        tenant_id: str | None,
        actor_id: str | None,
        correlation_id: str,
        idempotency_key: str | None = None,
        context: AdapterContext | None = None,
    ) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.service_token}",
            "X-Correlation-ID": correlation_id,
            "Accept": "application/json",
        }
        if tenant_id:
            headers["X-Tenant-ID"] = tenant_id
        if actor_id:
            headers["X-Actor-ID"] = actor_id
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        if context is not None:
            headers.update(context.outbound_headers())
        return headers

    @staticmethod
    def _safe_job(job: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "status": str(job.get("status", ""))[:64],
            "kind": str(job.get("kind", ""))[:64],
            "project_id": str(job.get("project_id", ""))[:128],
        }

    @staticmethod
    def _job_id(job: Any) -> str | None:
        if not isinstance(job, Mapping):
            return None
        value = job.get("id")
        if not isinstance(value, str) or not value or len(value) > 160:
            return None
        return value

    @staticmethod
    def _response_outcome(response: httpx.Response) -> AdapterResult:
        if response.status_code == 429:
            return AdapterResult(
                Outcome.TRANSIENT,
                error_class=ErrorClass.RETRYABLE_BEFORE_EFFECT,
                safe_error_code="media_rate_limited",
            )
        if response.status_code in {401, 403}:
            return AdapterResult(
                Outcome.REJECTED,
                error_class=ErrorClass.PROVIDER_AUTH,
                safe_error_code="media_service_auth_rejected",
            )
        if 400 <= response.status_code < 500:
            return AdapterResult(
                Outcome.REJECTED,
                error_class=ErrorClass.NON_RETRYABLE,
                safe_error_code=f"media_http_{response.status_code}",
            )
        return AdapterResult(
            Outcome.UNKNOWN,
            error_class=ErrorClass.AMBIGUOUS,
            safe_error_code=f"media_http_{response.status_code}",
        )

    async def readiness(self, context: AdapterContext) -> AdapterReadiness:
        if self.http is None:
            return AdapterReadiness(False, "http_client_unavailable")
        try:
            response = await self.http.get(
                f"{self.base_url}/readyz",
                headers=self._headers(
                    tenant_id=None,
                    actor_id=None,
                    correlation_id=context.correlation_id,
                    context=context,
                ),
                timeout=context.timeout_seconds,
            )
        except httpx.HTTPError as exc:
            return AdapterReadiness(False, type(exc).__name__)
        return AdapterReadiness(
            ready=response.status_code == 200,
            detail="ready" if response.status_code == 200 else f"http_{response.status_code}",
        )

    async def execute(
        self, command: CommandEnvelope, context: AdapterContext
    ) -> AdapterResult:
        kind = self.command_kinds.get(command.command_type)
        if kind is None:
            return AdapterResult(
                Outcome.UNSUPPORTED,
                error_class=ErrorClass.UNSUPPORTED,
                safe_error_code="unsupported_command_type",
            )
        project_id = command.payload.get("project_id")
        parameters = command.payload.get("parameters", {})
        if not isinstance(project_id, str) or not project_id.strip() or len(project_id) > 128:
            return AdapterResult(
                Outcome.REJECTED,
                error_class=ErrorClass.NON_RETRYABLE,
                safe_error_code="project_id_required",
            )
        if not isinstance(parameters, Mapping):
            return AdapterResult(
                Outcome.REJECTED,
                error_class=ErrorClass.NON_RETRYABLE,
                safe_error_code="parameters_must_be_object",
            )
        assert self.http is not None
        try:
            response = await self.http.post(
                f"{self.base_url}/v1/jobs",
                headers=self._headers(
                    tenant_id=command.tenant_id,
                    actor_id=command.requested_by,
                    correlation_id=command.correlation_id,
                    idempotency_key=command.idempotency_key,
                    context=context,
                ),
                json={
                    "kind": kind,
                    "project_id": project_id,
                    "parameters": dict(parameters),
                },
                timeout=context.timeout_seconds,
            )
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            return AdapterResult(
                Outcome.TRANSIENT,
                error_class=ErrorClass.RETRYABLE_BEFORE_EFFECT,
                safe_error_code=type(exc).__name__,
            )
        except httpx.HTTPError as exc:
            return AdapterResult(
                Outcome.UNKNOWN,
                error_class=ErrorClass.AMBIGUOUS,
                safe_error_code=type(exc).__name__,
            )
        if response.status_code not in {200, 201, 202}:
            return self._response_outcome(response)
        try:
            job = response.json()
        except ValueError:
            return AdapterResult(
                Outcome.UNKNOWN,
                error_class=ErrorClass.AMBIGUOUS,
                safe_error_code="media_invalid_json",
            )
        job_id = self._job_id(job)
        if job_id is None:
            return AdapterResult(
                Outcome.UNKNOWN,
                error_class=ErrorClass.AMBIGUOUS,
                safe_error_code="media_job_id_missing",
            )
        return AdapterResult(
            Outcome.ACCEPTED,
            provider_operation_id=job_id,
            safe_details=self._safe_job(job),
        )

    async def _get_job(
        self,
        operation: CommandOperation,
        context: AdapterContext,
    ) -> tuple[Mapping[str, Any] | None, ReadbackResult | None]:
        assert self.http is not None
        headers = self._headers(
            tenant_id=operation.tenant_id,
            actor_id=None,
            correlation_id=operation.correlation_id,
            context=context,
        )
        if operation.provider_operation_id:
            expected_id = operation.provider_operation_id
            url = f"{self.base_url}/v1/jobs/{quote(expected_id, safe='')}"
            params = None
        else:
            expected_id = None
            url = f"{self.base_url}/v1/jobs/by-idempotency"
            params = {"idempotency_key": operation.idempotency_key}
        try:
            response = await self.http.get(
                url,
                params=params,
                headers=headers,
                timeout=context.timeout_seconds,
            )
        except httpx.HTTPError as exc:
            return None, ReadbackResult(
                ReadbackStatus.UNAVAILABLE,
                provider_operation_id=operation.provider_operation_id,
                safe_error_code=type(exc).__name__,
            )
        if response.status_code == 404:
            return None, ReadbackResult(
                ReadbackStatus.NOT_FOUND,
                provider_operation_id=operation.provider_operation_id,
                safe_error_code="media_job_not_found",
            )
        if response.status_code != 200:
            return None, ReadbackResult(
                ReadbackStatus.UNAVAILABLE,
                provider_operation_id=operation.provider_operation_id,
                safe_error_code=f"media_http_{response.status_code}",
            )
        try:
            job = response.json()
        except ValueError:
            return None, ReadbackResult(
                ReadbackStatus.UNAVAILABLE,
                provider_operation_id=operation.provider_operation_id,
                safe_error_code="media_invalid_json",
            )
        if not isinstance(job, Mapping):
            return None, ReadbackResult(
                ReadbackStatus.UNAVAILABLE,
                provider_operation_id=operation.provider_operation_id,
                safe_error_code="media_invalid_job",
            )
        job_id = self._job_id(job)
        if job_id is None:
            return None, ReadbackResult(
                ReadbackStatus.UNAVAILABLE,
                provider_operation_id=operation.provider_operation_id,
                safe_error_code="media_job_id_missing",
            )
        if expected_id is not None and job_id != expected_id:
            return None, ReadbackResult(
                ReadbackStatus.MISMATCH,
                provider_operation_id=expected_id,
                safe_error_code="media_job_id_mismatch",
            )
        return job, None

    async def readback(
        self, operation: CommandOperation, context: AdapterContext
    ) -> ReadbackResult:
        job, error = await self._get_job(operation, context)
        if error is not None:
            return error
        assert job is not None
        job_id = self._job_id(job)
        state = str(job.get("status", "")).lower()
        evidence = self._safe_job(job)
        if state in _SUCCESS_STATES:
            status = ReadbackStatus.MATCHED
            code = None
        elif state in _FAILURE_STATES:
            status = ReadbackStatus.MISMATCH
            code = f"media_job_{state}"
        elif state in _PENDING_STATES:
            status = ReadbackStatus.UNAVAILABLE
            code = f"media_job_{state}"
        else:
            status = ReadbackStatus.UNAVAILABLE
            code = "media_job_unknown_state"
        return ReadbackResult(
            status,
            provider_operation_id=job_id or operation.provider_operation_id,
            evidence=evidence,
            safe_error_code=code,
        )

    async def status(
        self, operation: CommandOperation, context: AdapterContext
    ) -> AdapterResult:
        result = await self.readback(operation, context)
        if result.status is ReadbackStatus.MATCHED:
            return AdapterResult(
                Outcome.COMPLETED,
                provider_operation_id=result.provider_operation_id,
                safe_details=result.evidence,
            )
        if result.status is ReadbackStatus.MISMATCH:
            return AdapterResult(
                Outcome.REJECTED,
                provider_operation_id=result.provider_operation_id,
                error_class=ErrorClass.NON_RETRYABLE,
                safe_error_code=result.safe_error_code,
            )
        return AdapterResult(
            Outcome.UNKNOWN,
            provider_operation_id=result.provider_operation_id,
            error_class=ErrorClass.AMBIGUOUS,
            safe_error_code=result.safe_error_code,
        )

    async def cancel(
        self, operation: CommandOperation, context: AdapterContext
    ) -> AdapterResult:
        job_id = operation.provider_operation_id
        if not job_id:
            job, error = await self._get_job(operation, context)
            if error is not None:
                if error.status is ReadbackStatus.NOT_FOUND:
                    return AdapterResult(
                        Outcome.REJECTED,
                        error_class=ErrorClass.NON_RETRYABLE,
                        safe_error_code="media_job_not_found",
                    )
                return AdapterResult(
                    Outcome.UNKNOWN,
                    error_class=ErrorClass.AMBIGUOUS,
                    safe_error_code=error.safe_error_code,
                )
            assert job is not None
            job_id = self._job_id(job)
        if not job_id:
            return AdapterResult(
                Outcome.UNKNOWN,
                error_class=ErrorClass.AMBIGUOUS,
                safe_error_code="media_job_id_missing",
            )
        assert self.http is not None
        try:
            response = await self.http.post(
                f"{self.base_url}/v1/jobs/{quote(job_id, safe='')}/cancel",
                headers=self._headers(
                    tenant_id=operation.tenant_id,
                    actor_id=operation.requested_by,
                    correlation_id=operation.correlation_id,
                    idempotency_key=operation.idempotency_key,
                    context=context,
                ),
                timeout=context.timeout_seconds,
            )
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            return AdapterResult(
                Outcome.TRANSIENT,
                provider_operation_id=job_id,
                error_class=ErrorClass.RETRYABLE_BEFORE_EFFECT,
                safe_error_code=type(exc).__name__,
            )
        except httpx.HTTPError as exc:
            return AdapterResult(
                Outcome.UNKNOWN,
                provider_operation_id=job_id,
                error_class=ErrorClass.AMBIGUOUS,
                safe_error_code=type(exc).__name__,
            )
        if response.status_code != 200:
            result = self._response_outcome(response)
            return AdapterResult(
                result.outcome,
                provider_operation_id=job_id,
                error_class=result.error_class,
                safe_error_code=result.safe_error_code,
            )
        try:
            job = response.json()
        except ValueError:
            return AdapterResult(
                Outcome.UNKNOWN,
                provider_operation_id=job_id,
                error_class=ErrorClass.AMBIGUOUS,
                safe_error_code="media_invalid_json",
            )
        state = str(job.get("status", "")).lower() if isinstance(job, Mapping) else ""
        return AdapterResult(
            Outcome.COMPLETED if state == "cancelled" else Outcome.ACCEPTED,
            provider_operation_id=job_id,
            safe_details=self._safe_job(job) if isinstance(job, Mapping) else {},
        )


def media_saas_adapters(
    settings: Settings,
    *,
    http: httpx.AsyncClient | None,
) -> tuple[BaseAdapter, ...]:
    """Return only fully configured media bridges; missing configuration is inert."""

    specs = (
        (
            "blender-render",
            "blender",
            _BLENDER_COMMANDS,
            settings.blender_saas_base_url,
            settings.blender_saas_service_token,
        ),
        (
            "natron-compositor",
            "natron",
            _NATRON_COMMANDS,
            settings.natron_saas_base_url,
            settings.natron_saas_service_token,
        ),
        (
            "kdenlive-render",
            "kdenlive",
            _KDENLIVE_COMMANDS,
            settings.kdenlive_saas_base_url,
            settings.kdenlive_saas_service_token,
        ),
    )
    adapters: list[BaseAdapter] = []
    for adapter_id, family, commands, base_url, token in specs:
        try:
            candidate = MediaSaaSAdapter(
                adapter_id=adapter_id,
                provider_family=f"media-{family}",
                connector_ids=(adapter_id,),
                command_kinds=commands,
                base_url=base_url,
                service_token=token,
                http=http,
            )
            candidate.validate_config()
        except Exception as exc:  # noqa: BLE001 - missing/invalid media config fails closed
            logger.info(
                "media adapter %s not registered: %s",
                adapter_id,
                type(exc).__name__,
            )
            continue
        adapters.append(candidate)
    return tuple(adapters)


__all__ = [
    "MEDIA_RENDER_CAPABILITY",
    "MediaSaaSAdapter",
    "media_saas_adapters",
]
