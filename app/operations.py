from __future__ import annotations

import base64
import json
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .commands import (
    API_OPERATION_STATES,
    CommandOperation,
    OperationAttempt,
    OperationEvent,
    OperationMutationRequest,
)
from .control_plane_auth import caller_for_authorization
from .security import RequestValidationError, authorize_tenant
from .storage import StorageError

router = APIRouter(prefix="/v1/operations", tags=["operations"])
OperationApiState = Literal["RECEIVED", "QUEUED", "SUBMITTED", "ACCEPTED", "UNKNOWN", "COMPLETED", "FAILED", "RECONCILIATION_REQUIRED", "DEAD_LETTERED", "CANCELLED"]
_PERSISTED_BY_API_STATE = {value: key for key, value in API_OPERATION_STATES.items()}


class OperationResponse(CommandOperation):
    state: OperationApiState
    duplicate: bool


class OperationListResponse(BaseModel):
    items: list[OperationResponse]
    next_cursor: str | None = None


class OperationEventResponse(OperationEvent):
    previous_state: OperationApiState | None
    new_state: OperationApiState


class OperationEventListResponse(BaseModel):
    items: list[OperationEventResponse]
    next_cursor: str | None = None


class OperationAttemptResponse(OperationAttempt):
    state: OperationApiState


class OperationAttemptListResponse(BaseModel):
    items: list[OperationAttemptResponse]
    next_cursor: str | None = None


def _operation_json(operation) -> dict[str, Any]:
    payload = operation.model_dump(mode="json")
    payload["state"] = API_OPERATION_STATES[operation.state]
    return payload


def _encode_cursor(kind: str, values: list[Any]) -> str:
    raw = json.dumps({"v": 1, "kind": kind, "position": values}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(value: str | None, kind: str) -> list[Any] | None:
    if value is None: return None
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        data = json.loads(raw)
    except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise RequestValidationError("cursor is malformed") from exc
    if not isinstance(data, dict) or set(data) != {"v", "kind", "position"} or data["v"] != 1 or data["kind"] != kind or not isinstance(data["position"], list) or len(data["position"]) != 2:
        raise RequestValidationError("cursor is malformed")
    return data["position"]


async def _context(request: Request):
    active = request.app.state.runtime
    if active.commands is None: raise StorageError("command ledger is unavailable")
    tenant_id = request.headers.get("X-Tenant-ID", "")
    if not tenant_id: raise RequestValidationError("X-Tenant-ID is required")
    authorization = request.headers.get("Authorization", "")
    caller = caller_for_authorization(authorization)
    claims = await active.tokens.verify(authorization, expected_client_id=caller.client_id, required_scope=caller.status_scope)
    authorize_tenant(claims, tenant_id)
    return active.commands, tenant_id


async def _mutation_context(request: Request):
    active = request.app.state.runtime
    if active.commands is None: raise StorageError("command ledger is unavailable")
    tenant_id = request.headers.get("X-Tenant-ID", "")
    correlation_id = request.headers.get("X-Correlation-ID", "")
    idempotency_key = request.headers.get("Idempotency-Key", "")
    if not tenant_id or not correlation_id or not 8 <= len(idempotency_key) <= 180:
        raise RequestValidationError("X-Tenant-ID, X-Correlation-ID, and a bounded Idempotency-Key are required")
    authorization = request.headers.get("Authorization", "")
    caller = caller_for_authorization(authorization)
    claims = await active.tokens.verify(authorization, expected_client_id=caller.client_id, required_scope=caller.command_scope)
    authorize_tenant(claims, tenant_id)
    actor_id = claims.get("sub")
    if not isinstance(actor_id, str) or not actor_id: raise RequestValidationError("token subject is required")
    return active.commands, tenant_id, actor_id, idempotency_key


@router.get("", response_model=OperationListResponse)
async def list_operations(request: Request, limit: int = Query(50, ge=1, le=100), cursor: str | None = None, state: OperationApiState | None = None, command_type: str | None = Query(None, min_length=1, max_length=180)) -> JSONResponse:
    service, tenant_id = await _context(request)
    decoded = _decode_cursor(cursor, "operations")
    try: position = (datetime.fromisoformat(decoded[0]), UUID(decoded[1])) if decoded else None
    except (ValueError, TypeError) as exc: raise RequestValidationError("cursor is malformed") from exc
    rows = await service.list_operations(tenant_id, limit=limit + 1, position=position, state=_PERSISTED_BY_API_STATE[state] if state else None, command_type=command_type)
    more = len(rows) > limit
    items = rows[:limit]
    next_cursor = _encode_cursor("operations", [items[-1].created_at.isoformat(), str(items[-1].command_id)]) if more else None
    return JSONResponse(content={"items": [_operation_json(item) for item in items], "next_cursor": next_cursor})


@router.get("/{command_id}", response_model=OperationResponse)
async def get_operation(command_id: UUID, request: Request) -> JSONResponse:
    service, tenant_id = await _context(request)
    operation = await service.get(tenant_id, command_id)
    return JSONResponse(content=_operation_json(operation), headers={"X-Correlation-ID": operation.correlation_id})


@router.get("/{command_id}/events", response_model=OperationEventListResponse)
async def list_events(command_id: UUID, request: Request, limit: int = Query(50, ge=1, le=100), cursor: str | None = None) -> JSONResponse:
    service, tenant_id = await _context(request)
    decoded = _decode_cursor(cursor, "events")
    try: position = (datetime.fromisoformat(decoded[0]), int(decoded[1])) if decoded else None
    except (ValueError, TypeError) as exc: raise RequestValidationError("cursor is malformed") from exc
    rows = await service.list_events(tenant_id, command_id, limit=limit + 1, position=position)
    items, more = rows[:limit], len(rows) > limit
    next_cursor = _encode_cursor("events", [items[-1].created_at.isoformat(), items[-1].event_id]) if more else None
    payload = []
    for item in items:
        row = item.model_dump(mode="json")
        row["previous_state"] = API_OPERATION_STATES.get(item.previous_state, item.previous_state.upper() if item.previous_state else None)
        row["new_state"] = API_OPERATION_STATES.get(item.new_state, item.new_state.upper())
        payload.append(row)
    return JSONResponse(content={"items": payload, "next_cursor": next_cursor})


@router.get("/{command_id}/attempts", response_model=OperationAttemptListResponse)
async def list_attempts(command_id: UUID, request: Request, limit: int = Query(50, ge=1, le=100), cursor: str | None = None) -> JSONResponse:
    service, tenant_id = await _context(request)
    decoded = _decode_cursor(cursor, "attempts")
    try: position = (int(decoded[0]), int(decoded[1])) if decoded else None
    except (ValueError, TypeError) as exc: raise RequestValidationError("cursor is malformed") from exc
    rows = await service.list_attempts(tenant_id, command_id, limit=limit + 1, position=position)
    items, more = rows[:limit], len(rows) > limit
    next_cursor = _encode_cursor("attempts", [items[-1].attempt_number, items[-1].attempt_id]) if more else None
    payload = []
    for item in items:
        row = item.model_dump(mode="json")
        row["state"] = API_OPERATION_STATES.get(item.state, item.state.upper())
        payload.append(row)
    return JSONResponse(content={"items": payload, "next_cursor": next_cursor})


@router.post("/{command_id}/cancel", response_model=OperationResponse)
async def cancel_operation(command_id: UUID, body: OperationMutationRequest, request: Request) -> JSONResponse:
    service, tenant_id, actor_id, idempotency_key = await _mutation_context(request)
    operation = await service.mutate_operation(tenant_id, command_id, action="cancel", actor_id=actor_id, idempotency_key=idempotency_key, expected_version=body.expected_version, reason=body.reason)
    return JSONResponse(content=_operation_json(operation))


@router.post("/{command_id}/reconcile", response_model=OperationResponse)
async def reconcile_operation(command_id: UUID, body: OperationMutationRequest, request: Request) -> JSONResponse:
    service, tenant_id, actor_id, idempotency_key = await _mutation_context(request)
    operation = await service.mutate_operation(tenant_id, command_id, action="reconcile", actor_id=actor_id, idempotency_key=idempotency_key, expected_version=body.expected_version, reason=body.reason)
    return JSONResponse(content=_operation_json(operation))
