"""Canonical Middleware bridge to the standalone Leads Workstation service."""

from __future__ import annotations

from typing import Any, TypedDict
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import JSONResponse

from app.adapters.leads_workstation.client import (
    LeadsWorkstationClient,
    LeadsWorkstationConfigurationError,
    LeadsWorkstationUpstreamError,
    get_leads_workstation_client,
)
from app.core.platform_auth import require_platform_scope

router = APIRouter(prefix="/platform/v1/leads", tags=["leads-workstation"])


class RequestContext(TypedDict):
    idempotency_key: str
    request_id: str
    correlation_id: str
    causation_id: str
    traceparent: str


def _ctx(
    idempotency_key: str = "",
    request_id: str = "",
    correlation_id: str = "",
    causation_id: str = "",
    traceparent: str = "",
) -> RequestContext:
    request_id = request_id.strip() or str(uuid4())
    correlation_id = correlation_id.strip() or request_id
    causation_id = causation_id.strip() or request_id
    return {
        "idempotency_key": idempotency_key.strip(),
        "request_id": request_id,
        "correlation_id": correlation_id,
        "causation_id": causation_id,
        "traceparent": traceparent.strip(),
    }


def _response(response) -> JSONResponse:
    try:
        payload = response.json()
    except ValueError:
        payload = {"error": "invalid_upstream_response"}
    return JSONResponse(status_code=response.status_code, content=payload)


def _upstream_error(exc: Exception) -> HTTPException:
    if isinstance(exc, LeadsWorkstationConfigurationError):
        return HTTPException(503, "Leads Workstation integration is not configured")
    if isinstance(exc, LeadsWorkstationUpstreamError):
        return HTTPException(exc.status_code, exc.payload)
    return HTTPException(502, "Leads Workstation request failed")


@router.get(
    "",
    dependencies=[Depends(require_platform_scope("platform.leads.read"))],
)
async def list_leads(
    q: str | None = None,
    country: str | None = None,
    category: str | None = None,
    status: str | None = None,
    campaign_id: str | None = None,
    assigned_agent: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    client: LeadsWorkstationClient = Depends(get_leads_workstation_client),
):
    params = {
        key: value
        for key, value in {
            "q": q,
            "country": country,
            "category": category,
            "status": status,
            "campaign_id": campaign_id,
            "assigned_agent": assigned_agent,
            "limit": limit,
            "offset": offset,
        }.items()
        if value is not None
    }
    try:
        return _response(await client.request("GET", "/api/v2/leads", params=params))
    except Exception as exc:  # noqa: BLE001 - normalized at integration boundary
        raise _upstream_error(exc) from None


@router.get(
    "/campaigns",
    dependencies=[Depends(require_platform_scope("platform.leads.read"))],
)
async def list_campaigns(
    state: str | None = None,
    client: LeadsWorkstationClient = Depends(get_leads_workstation_client),
):
    try:
        params = {"state": state} if state else None
        return _response(
            await client.request("GET", "/api/v2/campaigns", params=params)
        )
    except Exception as exc:  # noqa: BLE001
        raise _upstream_error(exc) from None


@router.get(
    "/{lead_id}",
    dependencies=[Depends(require_platform_scope("platform.leads.read"))],
)
async def get_lead(
    lead_id: str,
    client: LeadsWorkstationClient = Depends(get_leads_workstation_client),
):
    try:
        return _response(await client.request("GET", f"/api/v2/leads/{lead_id}"))
    except Exception as exc:  # noqa: BLE001
        raise _upstream_error(exc) from None


@router.get(
    "/{lead_id}/contacts",
    dependencies=[Depends(require_platform_scope("platform.leads.read"))],
)
async def get_contacts(
    lead_id: str,
    client: LeadsWorkstationClient = Depends(get_leads_workstation_client),
):
    try:
        return _response(
            await client.request("GET", f"/api/v2/leads/{lead_id}/contacts")
        )
    except Exception as exc:  # noqa: BLE001
        raise _upstream_error(exc) from None


@router.post(
    "",
    dependencies=[Depends(require_platform_scope("platform.leads.write"))],
)
async def create_lead(
    body: dict[str, Any],
    idempotency_key: str = Header("", alias="Idempotency-Key"),
    request_id: str = Header("", alias="X-Codestra-Request-ID"),
    correlation_id: str = Header("", alias="X-Codestra-Correlation-ID"),
    causation_id: str = Header("", alias="X-Codestra-Causation-ID"),
    traceparent: str = Header("", alias="traceparent"),
    client: LeadsWorkstationClient = Depends(get_leads_workstation_client),
):
    context = _ctx(idempotency_key, request_id, correlation_id, causation_id, traceparent)
    try:
        return _response(
            await client.request("POST", "/api/v2/leads", payload=body, **context)
        )
    except (ValueError, LeadsWorkstationConfigurationError, LeadsWorkstationUpstreamError) as exc:
        raise _upstream_error(exc) if not isinstance(exc, ValueError) else HTTPException(422, str(exc)) from None


@router.patch(
    "/{lead_id}",
    dependencies=[Depends(require_platform_scope("platform.leads.write"))],
)
async def update_lead(
    lead_id: str,
    body: dict[str, Any],
    if_match: str = Header("", alias="If-Match"),
    idempotency_key: str = Header("", alias="Idempotency-Key"),
    request_id: str = Header("", alias="X-Codestra-Request-ID"),
    correlation_id: str = Header("", alias="X-Codestra-Correlation-ID"),
    causation_id: str = Header("", alias="X-Codestra-Causation-ID"),
    traceparent: str = Header("", alias="traceparent"),
    client: LeadsWorkstationClient = Depends(get_leads_workstation_client),
):
    if not if_match.strip():
        raise HTTPException(428, "If-Match is required")
    context = _ctx(idempotency_key, request_id, correlation_id, causation_id, traceparent)
    try:
        return _response(
            await client.request(
                "PATCH",
                f"/api/v2/leads/{lead_id}",
                payload=body,
                if_match=if_match,
                **context,
            )
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    except (LeadsWorkstationConfigurationError, LeadsWorkstationUpstreamError) as exc:
        raise _upstream_error(exc) from None


async def _action(
    lead_id: str,
    action: str,
    body: dict[str, Any],
    client: LeadsWorkstationClient,
    context: RequestContext,
):
    try:
        return _response(
            await client.request(
                "POST",
                f"/api/v2/leads/{lead_id}/{action}",
                payload=body,
                **context,
            )
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    except (LeadsWorkstationConfigurationError, LeadsWorkstationUpstreamError) as exc:
        raise _upstream_error(exc) from None


@router.post(
    "/{lead_id}/assign",
    dependencies=[Depends(require_platform_scope("platform.leads.write"))],
)
async def assign_lead(
    lead_id: str,
    body: dict[str, Any],
    idempotency_key: str = Header("", alias="Idempotency-Key"),
    request_id: str = Header("", alias="X-Codestra-Request-ID"),
    correlation_id: str = Header("", alias="X-Codestra-Correlation-ID"),
    causation_id: str = Header("", alias="X-Codestra-Causation-ID"),
    traceparent: str = Header("", alias="traceparent"),
    client: LeadsWorkstationClient = Depends(get_leads_workstation_client),
):
    return await _action(
        lead_id,
        "assign",
        body,
        client,
        _ctx(idempotency_key, request_id, correlation_id, causation_id, traceparent),
    )


@router.post(
    "/{lead_id}/transition",
    dependencies=[Depends(require_platform_scope("platform.leads.write"))],
)
async def transition_lead(
    lead_id: str,
    body: dict[str, Any],
    idempotency_key: str = Header("", alias="Idempotency-Key"),
    request_id: str = Header("", alias="X-Codestra-Request-ID"),
    correlation_id: str = Header("", alias="X-Codestra-Correlation-ID"),
    causation_id: str = Header("", alias="X-Codestra-Causation-ID"),
    traceparent: str = Header("", alias="traceparent"),
    client: LeadsWorkstationClient = Depends(get_leads_workstation_client),
):
    return await _action(
        lead_id,
        "transition",
        body,
        client,
        _ctx(idempotency_key, request_id, correlation_id, causation_id, traceparent),
    )


@router.post(
    "/{lead_id}/suppress",
    dependencies=[Depends(require_platform_scope("platform.leads.write"))],
)
async def suppress_lead(
    lead_id: str,
    body: dict[str, Any],
    idempotency_key: str = Header("", alias="Idempotency-Key"),
    request_id: str = Header("", alias="X-Codestra-Request-ID"),
    correlation_id: str = Header("", alias="X-Codestra-Correlation-ID"),
    causation_id: str = Header("", alias="X-Codestra-Causation-ID"),
    traceparent: str = Header("", alias="traceparent"),
    client: LeadsWorkstationClient = Depends(get_leads_workstation_client),
):
    return await _action(
        lead_id,
        "suppress",
        body,
        client,
        _ctx(idempotency_key, request_id, correlation_id, causation_id, traceparent),
    )


@router.post(
    "/{lead_id}/consent",
    dependencies=[Depends(require_platform_scope("platform.leads.write"))],
)
async def consent_lead(
    lead_id: str,
    body: dict[str, Any],
    idempotency_key: str = Header("", alias="Idempotency-Key"),
    request_id: str = Header("", alias="X-Codestra-Request-ID"),
    correlation_id: str = Header("", alias="X-Codestra-Correlation-ID"),
    causation_id: str = Header("", alias="X-Codestra-Causation-ID"),
    traceparent: str = Header("", alias="traceparent"),
    client: LeadsWorkstationClient = Depends(get_leads_workstation_client),
):
    return await _action(
        lead_id,
        "consent",
        body,
        client,
        _ctx(idempotency_key, request_id, correlation_id, causation_id, traceparent),
    )
