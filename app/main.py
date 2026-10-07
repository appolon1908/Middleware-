from __future__ import annotations

import uuid
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import AsyncIterator
from uuid import UUID

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError as FastApiValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import AwareDatetime, ValidationError

from .commands import CommandCapabilityDisabled, CommandEnvelope, CommandError
from .config import ConfigurationError, Settings
from .communications import (
    CommunicationEventPage,
    CommunicationMessage,
    CommunicationMessagePage,
    CommunicationUsageReport,
    CommunicationsConflict,
    CommunicationsError,
    CommunicationsNotFound,
    CommunicationsService,
    CreateMessageRequest,
    MemoryCommunicationsStore,
    Paged,
    ProviderHealthReport,
    ProviderReputationReport,
)
from .contracts import WEBHOOK_ROUTES, WebhookRoute
from . import provider_control_api as _provider_control_api  # noqa: F401
from .control_api import router as control_api_router
from .compatibility_api import router as compatibility_api_router
from .domain_api import router as domain_api_router
from .webhook_api import router as webhook_api_router
from .control_plane_auth import authorize_command, caller_for_authorization
from .lead_intake import (
    INTAKE_PRODUCER_CLIENT_ID,
    LeadSubmission,
    accept_lead_submission,
)
from .n8n_control_plane import router as n8n_control_plane_router
from .observability import (
    MiddlewareObservability,
    safe_correlation_id,
    safe_traceparent,
)
from .operations_dashboard import router as operations_dashboard_router
from .operations import OperationResponse, _operation_json, router as operations_router
from .runtime import Runtime, build_runtime
from .runtime_safety import RuntimeSafetyReadback, runtime_safety_readback
from .security import SecurityError
from .service import (
    CANONICAL_ERROR_SCHEMA,
    EVENT_TYPE_422_RESPONSE,
    IngressError,
    PayloadTooLargeError,
    ReplayConflictError,
    accept_webhook,
)
from .storage import ReplayConflict, StorageError
from .survey_routes import register_survey_routes



def _correlation_id(request: Request) -> str:
    assigned = getattr(request.state, "correlation_id", None)
    if isinstance(assigned, str):
        return assigned
    supplied = safe_correlation_id(request.headers.get("X-Correlation-ID"))
    if supplied is not None:
        return supplied
    return str(uuid.uuid4())


def _operation(request: Request) -> str:
    route = request.scope.get("route")
    template = getattr(route, "path", None)
    if isinstance(template, str) and template.startswith("/"):
        return template
    return "unmatched"


def _error_response(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
    retryable: bool,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "correlation_id": _correlation_id(request),
                "retryable": retryable,
                "details": {},
            }
        },
    )


async def _read_limited_body(request: Request, maximum: int) -> bytes:
    raw_length = request.headers.get("Content-Length")
    if raw_length is not None:
        try:
            length = int(raw_length)
        except ValueError as exc:
            from .security import RequestValidationError

            raise RequestValidationError("Content-Length must be an integer") from exc
        if length < 0:
            from .security import RequestValidationError

            raise RequestValidationError("Content-Length must not be negative")
        if length > maximum:
            raise PayloadTooLargeError(f"request body exceeds {maximum} bytes")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > maximum:
            raise PayloadTooLargeError(f"request body exceeds {maximum} bytes")
        body.extend(chunk)
    return bytes(body)


def create_app(
    *,
    settings: Settings | None = None,
    runtime: Runtime | None = None,
) -> FastAPI:
    resolved = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        active = runtime or await build_runtime(resolved)
        app.state.runtime = active
        try:
            yield
        finally:
            if runtime is None:
                await active.close()

    app = FastAPI(
        title="Codestra Middleware API",
        version=resolved.app_version,
        docs_url=None if resolved.app_env in {"staging", "production"} else "/docs",
        redoc_url=None,
        lifespan=lifespan,
    )
    telemetry = MiddlewareObservability(resolved)
    app.state.observability = telemetry
    app.include_router(n8n_control_plane_router)
    app.include_router(operations_dashboard_router)
    app.include_router(operations_router)
    app.include_router(control_api_router)
    app.include_router(compatibility_api_router)
    app.include_router(domain_api_router)
    app.include_router(webhook_api_router)

    @app.middleware("http")
    async def observe_request(request: Request, call_next):
        request.state.correlation_id = (
            safe_correlation_id(request.headers.get("X-Correlation-ID"))
            or str(uuid.uuid4())
        )
        request.state.traceparent = safe_traceparent(
            request.headers.get("traceparent")
        )
        started = telemetry.start_request()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers["X-Correlation-ID"] = request.state.correlation_id
            if request.state.traceparent is not None:
                response.headers["traceparent"] = request.state.traceparent
            return response
        finally:
            telemetry.finish_request(
                started=started,
                operation=_operation(request),
                method=request.method,
                status_code=status_code,
                correlation_id=request.state.correlation_id,
                traceparent=request.state.traceparent,
                intake_context=getattr(request.state, "intake_metrics", None),
            )

    @app.exception_handler(SecurityError)
    async def security_error(request: Request, exc: SecurityError) -> JSONResponse:
        request.app.state.observability.record_auth_denial(
            _operation(request),
            exc.code,
        )
        return _error_response(
            request,
            status_code=exc.status_code,
            code=exc.code,
            message=str(exc),
            retryable=exc.retryable,
        )

    @app.exception_handler(IngressError)
    async def ingress_error(request: Request, exc: IngressError) -> JSONResponse:
        return _error_response(
            request,
            status_code=exc.status_code,
            code=exc.code,
            message=str(exc),
            retryable=exc.retryable,
        )

    @app.exception_handler(StorageError)
    async def storage_error(request: Request, exc: StorageError) -> JSONResponse:
        return _error_response(
            request,
            status_code=503,
            code=exc.code,
            message="required persistence dependency is unavailable",
            retryable=exc.retryable,
        )

    @app.exception_handler(CommandError)
    async def command_error(request: Request, exc: CommandError) -> JSONResponse:
        return _error_response(
            request,
            status_code=exc.status_code,
            code=exc.code,
            message=str(exc),
            retryable=exc.retryable,
        )

    @app.exception_handler(CommunicationsError)
    async def communications_error(
        request: Request,
        exc: CommunicationsError,
    ) -> JSONResponse:
        return _error_response(
            request,
            status_code=exc.status_code,
            code=exc.code,
            message=str(exc),
            retryable=exc.retryable,
        )

    @app.exception_handler(FastApiValidationError)
    async def validation_error(
        request: Request,
        exc: FastApiValidationError,
    ) -> JSONResponse:
        return _error_response(
            request,
            status_code=400,
            code="invalid_request",
            message="request does not match the canonical API contract",
            retryable=False,
        )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {
            "status": "ok",
            "service": "middleware-api",
            "component": "api",
        }

    @app.get("/readiness")
    @app.get("/ready")
    async def ready(request: Request) -> JSONResponse:
        report = await request.app.state.runtime.readiness()
        request.app.state.observability.record_readiness(report.components)
        return JSONResponse(
            status_code=200 if report.ready else 503,
            content={
                "status": "ready" if report.ready else "not_ready",
                "service": "middleware-api",
                "component": "api",
                "environment": resolved.app_env,
                "release_sha": resolved.source_sha,
                "image_digest": resolved.image_digest,
                "schema_or_migration_head": resolved.schema_head,
                "checked_at": datetime.now(UTC).isoformat(),
                "components": report.components,
            },
        )

    @app.get("/dependencies")
    async def dependencies(request: Request) -> JSONResponse:
        report = await request.app.state.runtime.readiness()
        return JSONResponse(
            status_code=200 if report.ready else 503,
            content={
                "service": "middleware-api",
                "environment": resolved.app_env,
                "dependencies": report.components,
            },
        )

    @app.get("/capabilities")
    async def capabilities(request: Request) -> JSONResponse:
        safety = runtime_safety_readback(request.app.state.runtime.settings)
        effective = dict(safety["external_effects"])
        effective.update(safety["umbrella_controls"])
        effective["PRODUCTION_DIALING"] = safety["production_dialing"] == "ENABLED"
        return JSONResponse(
            status_code=200,
            content={"service": "middleware-api", "environment": resolved.app_env,
                     "capabilities": effective},
        )

    @app.get("/metrics")
    async def metrics(request: Request) -> Response:
        await request.app.state.runtime.tokens.verify(
            request.headers.get("Authorization", ""),
            expected_client_id="monitoring-readonly",
            required_scope="metrics.read",
        )
        report = await request.app.state.runtime.readiness()
        request.app.state.observability.record_readiness(report.components)
        await request.app.state.observability.refresh_intake_backlog(
            request.app.state.runtime.inbox
        )
        body, media_type = request.app.state.observability.render()
        return Response(content=body, headers={"Content-Type": media_type})

    @app.get("/version")
    async def version() -> dict[str, str]:
        return {
            "service": "middleware-api",
            "version": resolved.app_version,
            "release_id": resolved.release_id,
            "environment": resolved.app_env,
            "runtime_profile_id": (
                resolved.runtime_profile_id or "local-unlocked"
            ),
            "source_sha": resolved.source_sha,
            "git_sha": resolved.source_sha,
            "image_digest": resolved.image_digest,
            "schema_head": resolved.schema_head,
            "schema_version": resolved.schema_head,
            "build_time": resolved.build_time,
            "build_timestamp": resolved.build_time,
            "configuration_checksum": resolved.configuration_checksum,
        }

    @app.get("/v1/runtime/safety", response_model=RuntimeSafetyReadback)
    async def runtime_safety(request: Request) -> RuntimeSafetyReadback:
        await request.app.state.runtime.tokens.verify(
            request.headers.get("Authorization", ""),
            expected_client_id="monitoring-readonly",
            required_scope="health.read",
        )
        return RuntimeSafetyReadback.model_validate(
            runtime_safety_readback(request.app.state.runtime.settings)
        )

    def communications_service(request: Request) -> CommunicationsService:
        active = request.app.state.runtime
        if active.commands is None:
            raise StorageError("command ledger is unavailable")
        if active.communications is None:
            active.communications = CommunicationsService(
                store=MemoryCommunicationsStore(),
                commands=active.commands,
                umbrella_controls=active.settings.umbrella_controls,
            )
        return active.communications

    def _tenant_from_header(request: Request) -> str:
        tenant_id = request.headers.get("X-Tenant-ID", "")
        if not tenant_id:
            from .security import RequestValidationError

            raise RequestValidationError("X-Tenant-ID is required")
        return tenant_id

    async def _authorize_read(request: Request, tenant_id: str) -> None:
        authorization = request.headers.get("Authorization", "")
        caller = caller_for_authorization(authorization)
        claims = await request.app.state.runtime.tokens.verify(
            authorization,
            expected_client_id=caller.client_id,
            required_scope=caller.status_scope,
        )
        from .security import authorize_tenant

        authorize_tenant(claims, tenant_id)

    @app.post("/v1/communication/messages", response_model=CommunicationMessage, responses={202: {"model": CommunicationMessage}})
    @app.post("/v1/communications/messages", response_model=CommunicationMessage, responses={202: {"model": CommunicationMessage}})
    async def create_communication_message(
        body: CreateMessageRequest,
        request: Request,
    ) -> JSONResponse:
        tenant_id = _tenant_from_header(request)
        correlation_id = request.headers.get("X-Correlation-ID", "")
        idempotency_key = request.headers.get("Idempotency-Key", "")
        if not correlation_id or not idempotency_key:
            from .security import RequestValidationError

            raise RequestValidationError(
                "X-Correlation-ID and Idempotency-Key are required"
            )
        if len(correlation_id) > 180 or not 8 <= len(idempotency_key) <= 180:
            from .security import RequestValidationError

            raise RequestValidationError(
                "X-Correlation-ID or Idempotency-Key is outside contract bounds"
            )
        actor = request.headers.get("X-Codestra-Actor", "")
        if not actor:
            authorization = request.headers.get("Authorization", "")
            caller = caller_for_authorization(authorization)
            claims = await request.app.state.runtime.tokens.verify(
                authorization,
                expected_client_id=caller.client_id,
                required_scope=caller.command_scope,
            )
            actor = str(claims.get("sub") or "")
        message, duplicate = await communications_service(request).submit_message(
            body,
            tenant_id=tenant_id,
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
            actor=actor,
            authorization=request.headers.get("Authorization", ""),
            token_verifier=request.app.state.runtime.tokens,
        )
        return JSONResponse(
            status_code=200 if duplicate else 202,
            content=message.model_dump(mode="json"),
            headers={"X-Correlation-ID": message.correlationId},
        )

    @app.get("/v1/communications/messages", response_model=CommunicationMessagePage)
    async def list_communication_messages(request: Request) -> JSONResponse:
        tenant_id = _tenant_from_header(request)
        await _authorize_read(request, tenant_id)
        service = communications_service(request)
        return JSONResponse(
            status_code=200,
            content=Paged(
                items=[
                    item.model_dump(mode="json")
                    for item in service.list_messages(
                        tenant_id,
                        channel=request.query_params.get("channel"),
                        status=request.query_params.get("status"),
                    )
                ]
            ).model_dump(mode="json"),
        )

    @app.get("/v1/communication/messages/{messageId}", response_model=CommunicationMessage)
    @app.get("/v1/communications/messages/{messageId}", response_model=CommunicationMessage)
    async def get_communication_message(messageId: UUID, request: Request) -> JSONResponse:
        tenant_id = _tenant_from_header(request)
        await _authorize_read(request, tenant_id)
        service = communications_service(request)
        message = await service.refresh_command_status(tenant_id, messageId)
        return JSONResponse(
            status_code=200,
            content=message.model_dump(mode="json"),
        )

    @app.get("/v1/communications/messages/{messageId}/events", response_model=CommunicationEventPage)
    async def list_communication_message_events(
        messageId: UUID,
        request: Request,
    ) -> JSONResponse:
        tenant_id = _tenant_from_header(request)
        await _authorize_read(request, tenant_id)
        service = communications_service(request)
        await service.refresh_command_status(tenant_id, messageId)
        return JSONResponse(
            status_code=200,
            content=Paged(
                items=[
                    item.model_dump(mode="json")
                    for item in service.message_events(tenant_id, messageId)
                ]
            ).model_dump(mode="json"),
        )

    @app.post("/v1/communications/messages/{messageId}/cancel", response_model=CommunicationMessage, responses={202: {"model": CommunicationMessage}})
    async def cancel_communication_message(messageId: UUID, request: Request) -> JSONResponse:
        tenant_id = _tenant_from_header(request)
        idempotency_key = request.headers.get("Idempotency-Key", "")
        if not 8 <= len(idempotency_key) <= 180:
            from .security import RequestValidationError

            raise RequestValidationError(
                "Idempotency-Key must contain 8-180 characters"
            )
        authorization = request.headers.get("Authorization", "")
        caller = caller_for_authorization(authorization)
        claims = await request.app.state.runtime.tokens.verify(
            authorization,
            expected_client_id=caller.client_id,
            required_scope=caller.command_scope,
        )
        actor = request.headers.get("X-Codestra-Actor", "") or str(
            claims.get("sub") or ""
        )
        message, duplicate = await communications_service(request).cancel(
            tenant_id,
            messageId,
            idempotency_key=idempotency_key,
            actor=actor,
            authorization=authorization,
            token_verifier=request.app.state.runtime.tokens,
        )
        return JSONResponse(
            status_code=200 if duplicate else 202,
            content=message.model_dump(mode="json"),
        )

    @app.get("/v1/communications/provider-health", response_model=ProviderHealthReport)
    @app.get("/v1/communications/providers/health", response_model=ProviderHealthReport)
    async def get_communication_provider_health(request: Request) -> JSONResponse:
        tenant_id = _tenant_from_header(request)
        await _authorize_read(request, tenant_id)
        service = communications_service(request)
        return JSONResponse(status_code=200, content=await service.adapter.health(tenant_id))

    @app.get("/v1/communications/reputation", response_model=ProviderReputationReport)
    async def get_communication_reputation(request: Request) -> JSONResponse:
        tenant_id = _tenant_from_header(request)
        await _authorize_read(request, tenant_id)
        service = communications_service(request)
        return JSONResponse(status_code=200, content=await service.adapter.reputation(tenant_id))

    @app.get("/v1/communications/usage", response_model=CommunicationUsageReport)
    async def get_communication_usage(
        request: Request,
        from_: AwareDatetime | None = Query(None, alias="from"),
        to: AwareDatetime | None = Query(None),
    ) -> JSONResponse:
        tenant_id = _tenant_from_header(request)
        await _authorize_read(request, tenant_id)
        messages = [
            item
            for item in communications_service(request).list_messages(tenant_id)
            if item.direction == "outbound"
        ]
        return JSONResponse(
            status_code=200,
            content={
                "from": (from_ or datetime.now(UTC)).isoformat(),
                "to": (to or datetime.now(UTC)).isoformat(),
                "totals": [
                    {
                        "channel": channel,
                        "accepted": len(
                            [item for item in messages if item.channel == channel]
                        ),
                        "delivered": len(
                            [
                                item
                                for item in messages
                                if item.channel == channel
                                and item.status == "delivered"
                            ]
                        ),
                        "failed": len(
                            [
                                item
                                for item in messages
                                if item.channel == channel and item.status == "failed"
                            ]
                        ),
                        "suppressed": len(
                            [
                                item
                                for item in messages
                                if item.channel == channel
                                and item.status == "suppressed"
                            ]
                        ),
                    }
                    for channel in ("email", "sms")
                ],
            },
        )
    @app.post("/v1/intake/leads")
    async def submit_lead(request: Request) -> JSONResponse:
        from .security import RequestValidationError, authorize_tenant

        active = request.app.state.runtime
        content_type = request.headers.get("Content-Type", "")
        if content_type.split(";", 1)[0].strip().lower() != "application/json":
            raise RequestValidationError("Content-Type must be application/json")

        tenant_id = request.headers.get("X-Tenant-ID", "")
        correlation_id = request.headers.get("X-Correlation-ID", "")
        idempotency_key = request.headers.get("Idempotency-Key", "")
        if not tenant_id:
            raise RequestValidationError("X-Tenant-ID is required")
        if not correlation_id or len(correlation_id) > 180:
            raise RequestValidationError("X-Correlation-ID must contain 1 to 180 characters")
        if not idempotency_key or not 8 <= len(idempotency_key) <= 180:
            raise RequestValidationError("Idempotency-Key must contain 8 to 180 characters")

        claims = await active.tokens.verify(
            request.headers.get("Authorization", ""),
            expected_client_id=INTAKE_PRODUCER_CLIENT_ID,
            required_scope="leads.write",
        )
        authorize_tenant(claims, tenant_id)

        raw = await _read_limited_body(request, active.settings.max_request_body_bytes)
        try:
            submission = LeadSubmission.model_validate_json(raw)
        except (ValidationError, ValueError) as exc:
            raise RequestValidationError(
                "body does not match the canonical lead intake contract"
            ) from exc
        request.state.intake_metrics = {
            "channel": submission.source,
            "form_kind": "configured" if submission.formId else "generic",
        }
        if submission.tenantId != tenant_id:
            raise RequestValidationError("X-Tenant-ID does not match submission tenantId")

        try:
            result = await accept_lead_submission(
                active,
                submission,
                idempotency_key=idempotency_key,
                correlation_id=correlation_id,
            )
        except ReplayConflict as exc:
            raise ReplayConflictError(str(exc)) from exc

        return JSONResponse(
            status_code=200 if result.duplicate else 202,
            content=result.model_dump(mode="json"),
            headers={"X-Correlation-ID": result.correlation_id},
        )

    register_survey_routes(app)

    @app.post(
        "/v1/commands",
        response_model=OperationResponse,
        responses={202: {"model": OperationResponse, "description": "Command accepted"}},
    )
    async def submit_command(
        command: CommandEnvelope,
        request: Request,
    ) -> JSONResponse:
        active = request.app.state.runtime
        if active.commands is None:
            raise StorageError("command ledger is unavailable")
        authorization = request.headers.get("Authorization", "")
        caller = caller_for_authorization(authorization)
        claims = await active.tokens.verify(
            authorization,
            expected_client_id=caller.client_id,
            required_scope=caller.command_scope,
        )
        authorize_command(
            caller,
            command_type=command.command_type,
            target=command.target,
        )
        from .security import authorize_tenant

        authorize_tenant(claims, command.tenant_id)
        if request.headers.get("X-Tenant-ID") != command.tenant_id:
            from .security import RequestValidationError

            raise RequestValidationError("X-Tenant-ID does not match command tenant")
        if request.headers.get("X-Correlation-ID") != command.correlation_id:
            from .security import RequestValidationError

            raise RequestValidationError(
                "X-Correlation-ID does not match command correlation_id"
            )
        if request.headers.get("Idempotency-Key") != command.idempotency_key:
            from .security import RequestValidationError

            raise RequestValidationError(
                "Idempotency-Key does not match command idempotency_key"
            )
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject:
            from .security import AuthorizationError

            raise AuthorizationError("token subject is required for commands")
        if (
            caller.client_id == "n8n-automation"
            and active.settings.umbrella_controls.get(
                "N8N_EXTERNAL_PROVIDER_WRITES"
            )
            is not True
        ):
            raise CommandCapabilityDisabled(
                "N8N_EXTERNAL_PROVIDER_WRITES is disabled"
            )
        operation = await active.commands.submit(
            command,
            authenticated_subject=subject,
            authenticated_client_id=caller.client_id,
        )
        status_code = 200 if operation.duplicate else 202
        return JSONResponse(
            status_code=status_code,
            content=_operation_json(operation),
            headers={
                "Location": f"/v1/operations/{operation.command_id}",
                "X-Correlation-ID": operation.correlation_id,
            },
        )

    def register(route: WebhookRoute) -> None:
        async def ingress(request: Request) -> JSONResponse:
            headers = {key.lower(): value for key, value in request.headers.items()}
            claims = await request.app.state.runtime.tokens.verify(
                headers.get("authorization", ""),
                expected_client_id=route.producer_client_id,
                required_scope=route.required_scope,
            )
            raw = await _read_limited_body(
                request,
                request.app.state.runtime.settings.max_request_body_bytes,
            )
            result, status_code = await accept_webhook(
                request.app.state.runtime,
                route,
                claims=claims,
                method=request.method,
                path=request.url.path,
                raw_body=raw,
                headers=headers,
            )
            if (
                route.producer_client_id
                in {"klyrow-gateway", "telnexa-gateway"}
                and request.app.state.runtime.communications is not None
            ):
                from .models import EventEnvelope

                envelope = EventEnvelope.model_validate(json.loads(raw))
                await request.app.state.runtime.communications.record_provider_event(envelope)
            return JSONResponse(
                status_code=status_code,
                content=result.model_dump(mode="json"),
            )

        app.add_api_route(
            route.path,
            ingress,
            methods=["POST"],
            name=f"ingress-{route.producer_client_id}-{route.path.rsplit('/', 1)[-1]}",
            responses={422: EVENT_TYPE_422_RESPONSE},
        )

    for webhook_route in WEBHOOK_ROUTES:
        register(webhook_route)

    generated_openapi = app.openapi

    def canonical_openapi() -> dict:
        schema = generated_openapi()
        automatic_validation = {
            "description": "Validation Error",
            "content": {
                "application/json": {
                    "schema": {"$ref": "#/components/schemas/HTTPValidationError"}
                }
            },
        }
        for path_item in schema.get("paths", {}).values():
            for operation in path_item.values():
                if not isinstance(operation, dict):
                    continue
                responses = operation.get("responses", {})
                if responses.get("422") != automatic_validation:
                    continue
                responses.pop("422")
                invalid = responses.setdefault(
                    "400", {"description": "Invalid canonical request"}
                )
                invalid["content"] = {
                    "application/json": {"schema": CANONICAL_ERROR_SCHEMA}
                }
        return schema

    app.openapi = canonical_openapi

    return app


try:
    app = create_app()
except ConfigurationError:
    app = None
