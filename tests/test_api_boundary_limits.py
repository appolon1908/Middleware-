from __future__ import annotations

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.api_inputs import optional_header, required_header
from app.core.request_guard import RequestGuard, install_request_guard


@pytest.mark.parametrize(
    "value", ["   ", "corr\r\ninjected", "corr\x00id", " corr", "corr ", "café"]
)
@pytest.mark.parametrize("reader", [required_header, optional_header])
def test_identity_headers_reject_whitespace_and_controls(value, reader):
    request = Request(
        {"type": "http", "headers": [(b"x-correlation-id", value.encode())]}
    )
    from app.security import RequestValidationError

    with pytest.raises(RequestValidationError):
        reader(request, "X-Correlation-ID", minimum=1, maximum=180)


def test_control_plane_rejects_oversized_body_before_parsing(test_settings):
    from fastapi import APIRouter

    router = APIRouter()

    @router.post("/platform/v1/probe")
    async def probe(body: dict):
        return body

    settings = test_settings.model_copy(update={"max_request_body_bytes": 32})
    app = FastAPI()
    app.include_router(router)
    install_request_guard(
        app, RequestGuard(settings, handler_authenticated_routers=(router,))
    )
    with TestClient(app) as client:
        response = client.post(
            "/platform/v1/probe",
            content=b'{"value":"' + b"x" * 64 + b'"}',
            headers={"Content-Type": "application/json"},
        )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"
    assert (
        response.headers["X-Correlation-ID"]
        == response.json()["error"]["correlation_id"]
    )


@pytest.mark.parametrize(
    "raw", [b'{"value":1,"value":2}', b'{"nested":{"x":1,"x":2}}', b'{"value":NaN}']
)
def test_control_plane_rejects_ambiguous_json(raw, test_settings):
    from fastapi import APIRouter
    from app.appolon_routes import install_error_handlers

    router = APIRouter()

    @router.post("/platform/v1/probe")
    async def probe(body: dict):
        return body

    app = FastAPI()
    app.include_router(router)
    install_error_handlers(app)
    install_request_guard(
        app, RequestGuard(test_settings, handler_authenticated_routers=(router,))
    )
    with TestClient(app) as client:
        response = client.post(
            "/platform/v1/probe",
            content=raw,
            headers={"Content-Type": "application/json"},
        )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


@pytest.mark.parametrize("length_headers", [{}, {"Content-Length": "1"}])
def test_control_plane_bounds_stream_without_content_length(
    test_settings, length_headers
):
    import asyncio
    from app.appolon_routes import install_error_handlers
    from fastapi import APIRouter

    router = APIRouter()

    @router.post("/platform/v1/probe")
    async def probe(body: dict):
        raise AssertionError("oversized body reached the handler")

    app = FastAPI()
    app.include_router(router)
    install_error_handlers(app)
    install_request_guard(
        app,
        RequestGuard(
            test_settings.model_copy(update={"max_request_body_bytes": 32}),
            handler_authenticated_routers=(router,),
        ),
    )
    import httpx

    async def run():
        async def chunks():
            yield b'{"value":"'
            yield b"x" * 64
            yield b'"}'

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.post(
                "/platform/v1/probe",
                content=chunks(),
                headers={"Content-Type": "application/json", **length_headers},
            )

    response = asyncio.run(run())
    assert response.status_code == 413


def test_boundary_rejection_records_status_without_body_or_token(test_settings):
    from fastapi import APIRouter
    from unittest.mock import Mock

    router = APIRouter()

    @router.post("/platform/v1/probe")
    async def probe(body: dict):
        return body

    telemetry = Mock()
    telemetry.start_request.return_value = 0.0
    app = FastAPI()
    app.include_router(router)
    install_request_guard(
        app,
        RequestGuard(
            test_settings.model_copy(update={"max_request_body_bytes": 4}),
            handler_authenticated_routers=(router,),
            telemetry=telemetry,
        ),
    )
    with TestClient(app) as client:
        response = client.post(
            "/platform/v1/probe",
            content=b'{"private":"payload"}',
            headers={"Content-Type": "application/json"},
        )
    assert response.status_code == 413
    telemetry.finish_request.assert_called_once()
    record = telemetry.finish_request.call_args.kwargs
    assert record["status_code"] == 413
    assert record["operation"] == "api.boundary"
    assert "private" not in str(record)


@pytest.mark.parametrize(
    "name", ["Authorization", "X-Tenant-ID", "X-Correlation-ID", "Idempotency-Key"]
)
@pytest.mark.parametrize("values", [[" "], ["value\x00"], ["one", "two"]])
def test_boundary_rejects_malformed_headers_before_handler(name, values, test_settings):
    from fastapi import APIRouter

    router = APIRouter()

    @router.get("/platform/v1/probe")
    async def probe():
        raise AssertionError("malformed identity reached handler")

    app = FastAPI()
    app.include_router(router)
    install_request_guard(app, RequestGuard(test_settings, boundary_routers=(router,)))
    with TestClient(app) as client:
        response = client.get(
            "/platform/v1/probe", headers=[(name, value) for value in values]
        )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
    assert (
        response.headers["X-Correlation-ID"]
        == response.json()["error"]["correlation_id"]
    )


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/platform/v1/services"),
        ("POST", "/v2/automation/commands"),
        ("POST", "/platform/v1/commands"),
        ("GET", "/v1/connectors"),
    ],
)
def test_canonical_families_bound_bodies_before_authentication(
    method, path, test_settings
):
    from app.application import AppProfile, create_app

    app = create_app(
        settings=test_settings.model_copy(update={"max_request_body_bytes": 32}),
        profile=AppProfile.MONOLITH,
    )
    # No lifespan or external infrastructure is needed for a boundary rejection.
    response = TestClient(app).request(
        method, path, content=b"{" * 64, headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"


def test_boundary_registration_does_not_bypass_authentication(test_settings):
    from fastapi import APIRouter

    router = APIRouter()

    @router.post("/api/v1/probe")
    async def probe(body: dict):
        return body

    guard = RequestGuard(test_settings, boundary_routers=(router,))
    assert not guard.handler_authenticated("POST", "/api/v1/probe")
