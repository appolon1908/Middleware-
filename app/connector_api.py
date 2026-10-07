"""HTTP facade over the canonical connector management domain.

The facade validates the published contract and the original caller token,
then delegates without performing installations, lifecycle changes or effects.
The connector runtime remains the sole owner of domain state and effect gates.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import yaml
from fastapi import APIRouter, Request
from fastapi.responses import Response
from jsonschema import Draft202012Validator, FormatChecker, ValidationError

from app.api_inputs import authorization_header, optional_header
from app.appolon_routes import read_limited_body
from app.security import AuthorizationError, RequestValidationError
from app.storage import StorageError
from app.service import CANONICAL_ERROR_SCHEMA

ROOT = Path(__file__).resolve().parents[1] / "contracts" / "connectors"
CONTRACT = yaml.safe_load((ROOT / "connector-management-api.v1.yaml").read_text())
MAXIMUM_BYTES = 1_048_576
router = APIRouter(tags=["connector-management-facade"])


def _expand(value: Any, document: dict = CONTRACT) -> Any:
    """Inline only local, source-controlled references for validation/OpenAPI."""
    if isinstance(value, list):
        return [_expand(item, document) for item in value]
    if not isinstance(value, dict):
        return value
    if "$ref" in value:
        ref = value["$ref"]
        filename, _, pointer = ref.partition("#")
        target = document
        if filename:
            path = (ROOT / filename).resolve()
            if path.parent != ROOT or path.suffix != ".json":
                raise ValueError("connector contract reference must be local JSON")
            target = json.loads(path.read_text())
        node = target
        for segment in pointer.lstrip("/").split("/") if pointer else ():
            node = node[segment.replace("~1", "/").replace("~0", "~")]
        return _expand(node, target)
    return {
        key: _expand(item, document)
        for key, item in value.items()
        if key not in {"$id", "$schema"}
    }


def _validate(schema: dict, value: Any) -> None:
    try:
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(value)
    except (ValidationError, ValueError, TypeError, RecursionError) as exc:
        raise RequestValidationError(
            "request does not match the connector API contract"
        ) from exc


def _tenant(claims: dict) -> str:
    values = claims.get("tenant_ids", [])
    if not isinstance(values, list):
        raise AuthorizationError("connector tenant claims are malformed")
    values = values + ([claims["tenant_id"]] if "tenant_id" in claims else [])
    try:
        tenants = {str(UUID(value)) for value in values if isinstance(value, str)}
    except ValueError as exc:
        raise AuthorizationError("connector tenant claims must be UUIDs") from exc
    if len(tenants) != 1 or any(not isinstance(value, str) for value in values):
        raise AuthorizationError("connector API requires exactly one authorized tenant")
    if not isinstance(claims.get("sub"), str) or not claims["sub"].strip():
        raise AuthorizationError("connector token subject is required")
    return next(iter(tenants))


def _handler(operation: dict, method: str):
    scope = operation["security"][0]["oauth2"][0]

    async def delegate(request: Request) -> Response:
        runtime = getattr(request.app.state, "runtime", None)
        if runtime is None:
            raise StorageError("connector API runtime is unavailable")
        authorization = authorization_header(request)
        claims = await runtime.tokens.verify(
            authorization,
            expected_client_id="connector-management-api",
            required_scope=scope,
        )
        tenant = _tenant(claims)
        requested_tenant = optional_header(
            request, "X-Tenant-ID", minimum=1, maximum=128
        )
        if requested_tenant is not None and requested_tenant != tenant:
            raise AuthorizationError("token is not authorized for the requested tenant")
        correlation = optional_header(
            request, "X-Correlation-ID", minimum=1, maximum=180
        ) or str(uuid4())
        forwarded = {"Authorization": authorization, "X-Correlation-ID": correlation}
        if method in {"POST", "PATCH"}:
            forwarded["Content-Type"] = "application/json"
        parameters = operation.get("parameters", [])
        allowed_query = {p["name"] for p in parameters if p["in"] == "query"}
        if set(request.query_params) - allowed_query:
            raise RequestValidationError("unsupported connector query parameter")
        for parameter in parameters:
            value: Any
            name, location = parameter["name"], parameter["in"]
            if location == "header":
                value = optional_header(request, name, minimum=1, maximum=8192)
                if name == "X-Correlation-ID":
                    value = correlation
                if value is not None:
                    forwarded[name] = value
            elif location == "query":
                values = request.query_params.getlist(name)
                if len(values) > 1:
                    raise RequestValidationError(
                        "connector query parameter must occur at most once"
                    )
                value = values[0] if values else None
            else:
                value = request.path_params.get(name)
            if value is None:
                if parameter.get("required"):
                    raise RequestValidationError(
                        "required connector parameter is missing"
                    )
                continue
            schema = parameter.get("schema", {})
            if schema.get("type") == "integer":
                try:
                    value = int(value)
                except ValueError as exc:
                    raise RequestValidationError(
                        "connector query parameter must be an integer"
                    ) from exc
            _validate(schema, value)
        body = await read_limited_body(request, MAXIMUM_BYTES)
        request_body = operation.get("requestBody")
        if request_body is not None:
            try:
                value = json.loads(body)
            except (ValueError, UnicodeError, RecursionError) as exc:
                raise RequestValidationError(
                    "connector request body must be JSON"
                ) from exc
            _validate(request_body["content"]["application/json"]["schema"], value)
        elif body:
            raise RequestValidationError(
                "connector operation does not accept a request body"
            )
        base = runtime.settings.connector_management_base_url
        if not base or runtime.http is None:
            raise StorageError(
                "canonical connector management upstream is not configured"
            )
        try:
            async with runtime.http.stream(
                method,
                base.rstrip("/") + request.url.path,
                params=request.query_params.multi_items(),
                headers=forwarded,
                content=body,
                follow_redirects=False,
            ) as upstream:
                if 300 <= upstream.status_code < 400:
                    raise StorageError("connector upstream redirect is forbidden")
                raw = bytearray()
                async for chunk in upstream.aiter_bytes():
                    if len(raw) + len(chunk) > MAXIMUM_BYTES:
                        raise StorageError(
                            "connector upstream response exceeds the boundary limit"
                        )
                    raw.extend(chunk)
                response_contract = operation["responses"].get(
                    str(upstream.status_code), operation["responses"].get("default")
                )
                media: dict[str, Any] = (response_contract or {}).get("content", {})
                media_contract: dict[str, Any] = next(iter(media.values()), {})
                schema = media_contract.get("schema")
                if schema is not None:
                    try:
                        _validate(schema, json.loads(raw))
                    except (RequestValidationError, ValueError, UnicodeError) as exc:
                        raise StorageError(
                            "connector upstream response violates its contract"
                        ) from exc
                headers = {
                    name: upstream.headers[name]
                    for name in ("ETag", "Retry-After", "Content-Type")
                    if name in upstream.headers
                }
                headers["X-Correlation-ID"] = correlation
                headers["Cache-Control"] = "no-store"
                return Response(
                    bytes(raw), status_code=upstream.status_code, headers=headers
                )
        except httpx.HTTPError as exc:
            raise StorageError(
                "canonical connector management upstream is unavailable"
            ) from exc

    return delegate


for path, operations in CONTRACT["paths"].items():
    for method, raw in operations.items():
        if method not in {"get", "post", "patch", "delete"} or not raw.get("security"):
            continue  # Signed webhook ingress stays with its existing authority.
        operation = _expand(raw)
        # The machine JWT is verified by the canonical runtime, then forwarded
        # unchanged so the domain independently enforces its own effect gates.
        extra = dict(operation)
        extra["security"] = [{"connectorFacadeBearer": []}]
        extra["x-required-scope"] = operation["security"][0]["oauth2"][0]
        for code in ("400", "401", "403", "413", "503"):
            extra.setdefault("responses", {}).setdefault(
                code,
                {
                    "description": "Middleware API boundary denial or dependency failure",
                    "content": {"application/json": {"schema": CANONICAL_ERROR_SCHEMA}},
                },
            )
        router.add_api_route(
            path,
            _handler(operation, method.upper()),
            methods=[method.upper()],
            operation_id="facade_" + operation["operationId"],
            openapi_extra=extra,
            response_model=None,
        )
