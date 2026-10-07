from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .commands import CommandCapabilityDisabled, CommandEnvelope
from .odoo_provider_adapter import OdooProviderAdapter, OdooProviderAdapterError
from .security import AuthorizationError, RequestValidationError, authorize_tenant
from .storage import StorageError

router = APIRouter(prefix="/v1/integrations/n8n", tags=["n8n-control-plane"])

_LEGACY_SUNSET = "Wed, 30 Jun 2027 23:59:59 GMT"
_LEGACY_WARNING = '299 - "Deprecated n8n v1 compatibility route; migrate to /v2/automation"'
_COMMAND_SUCCESSOR = '</v2/automation/commands>; rel="successor-version"'


def _legacy_headers(*, successor: str) -> dict[str, str]:
    return {
        "Deprecation": "true",
        "Sunset": _LEGACY_SUNSET,
        "Warning": _LEGACY_WARNING,
        "Link": successor,
    }


def _require_forwarding_headers(request: Request, command: CommandEnvelope) -> None:
    if request.headers.get("X-Tenant-ID") != command.tenant_id:
        raise RequestValidationError("X-Tenant-ID does not match command tenant")
    if request.headers.get("X-Correlation-ID") != command.correlation_id:
        raise RequestValidationError(
            "X-Correlation-ID does not match command correlation_id"
        )
    if request.headers.get("Idempotency-Key") != command.idempotency_key:
        raise RequestValidationError(
            "Idempotency-Key does not match command idempotency_key"
        )


def _validate_destination_contract(command: CommandEnvelope) -> None:
    """Reject destination-specific deterministic errors before ledger acceptance."""

    if command.target != "odoo-19":
        return
    try:
        OdooProviderAdapter._validate_command_document(
            command.model_dump(mode="json")
        )
    except OdooProviderAdapterError as exc:
        raise RequestValidationError(str(exc)) from exc


@router.post("/commands", deprecated=True)
async def submit_n8n_command(command: CommandEnvelope, request: Request) -> JSONResponse:
    """Accept a durable command through the legacy n8n v1 compatibility route.

    Kong remains the network/API gateway. Middleware independently validates the
    original Keycloak token so a gateway routing mistake cannot grant write
    authority. The canonical successor is the lease-bound v2 automation API.
    """
    active = request.app.state.runtime
    if active.commands is None:
        raise StorageError("command ledger is unavailable")
    claims = await active.tokens.verify(
        request.headers.get("Authorization", ""),
        expected_client_id="n8n-automation",
        required_scope="middleware.request.forward",
    )
    authorize_tenant(claims, command.tenant_id)
    _require_forwarding_headers(request, command)
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject:
        raise AuthorizationError("token subject is required for commands")
    _validate_destination_contract(command)
    if (
        active.settings.umbrella_controls.get("N8N_EXTERNAL_PROVIDER_WRITES")
        is not True
    ):
        raise CommandCapabilityDisabled("N8N_EXTERNAL_PROVIDER_WRITES is disabled")
    operation = await active.commands.submit(
        command,
        authenticated_subject=subject,
        authenticated_client_id="n8n-automation",
    )
    status_code = 200 if operation.duplicate else 202
    headers = {
        "Location": f"/v1/integrations/n8n/operations/{operation.command_id}",
        "X-Correlation-ID": operation.correlation_id,
        **_legacy_headers(successor=_COMMAND_SUCCESSOR),
    }
    return JSONResponse(
        status_code=status_code,
        content=operation.model_dump(mode="json"),
        headers=headers,
    )


@router.get("/operations/{command_id}", deprecated=True)
async def get_n8n_operation(command_id: UUID, request: Request) -> JSONResponse:
    """Return durable command state through the legacy n8n v1 compatibility route."""
    active = request.app.state.runtime
    if active.commands is None:
        raise StorageError("command ledger is unavailable")
    claims = await active.tokens.verify(
        request.headers.get("Authorization", ""),
        expected_client_id="n8n-automation",
        required_scope="middleware.status.read",
    )
    tenant_id = request.headers.get("X-Tenant-ID", "")
    authorize_tenant(claims, tenant_id)
    operation = await active.commands.get(tenant_id, command_id)
    return JSONResponse(
        status_code=200,
        content=operation.model_dump(mode="json"),
        headers={
            "X-Correlation-ID": operation.correlation_id,
            **_legacy_headers(
                successor=f'</v2/automation/commands/{command_id}>; rel="successor-version"'
            ),
        },
    )
