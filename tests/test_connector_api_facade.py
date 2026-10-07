from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.appolon_routes import install_error_handlers
from app.connector_api import router

TENANT = str(uuid4())
CORRELATION = str(uuid4())


def make_client(handler=None, configured=True):
    app = FastAPI()
    app.include_router(router)
    install_error_handlers(app)
    tokens = SimpleNamespace(
        verify=AsyncMock(return_value={"sub": "actor", "tenant_id": TENANT})
    )
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(
            handler
            or (
                lambda request: httpx.Response(
                    200,
                    json={
                        "data": [],
                        "meta": {"api_version": "v1", "correlation_id": CORRELATION},
                        "next_cursor": None,
                    },
                )
            )
        )
    )
    app.state.runtime = SimpleNamespace(
        tokens=tokens,
        http=http,
        settings=SimpleNamespace(
            connector_management_base_url="http://connector-runtime:8097"
            if configured
            else None
        ),
    )
    return TestClient(app), tokens, http


def headers():
    return {"Authorization": "Bearer synthetic", "X-Correlation-ID": CORRELATION}


def test_connector_catalog_delegates_verified_identity_and_query():
    requests = []

    def upstream(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "data": [],
                "meta": {"api_version": "v1", "correlation_id": CORRELATION},
                "next_cursor": None,
            },
        )

    client, tokens, _ = make_client(upstream)
    with client:
        response = client.get("/v1/connectors?limit=5", headers=headers())
    assert response.status_code == 200
    tokens.verify.assert_awaited_once_with(
        "Bearer synthetic",
        expected_client_id="connector-management-api",
        required_scope="connector.catalog.read",
    )
    assert str(requests[0].url) == "http://connector-runtime:8097/v1/connectors?limit=5"
    assert requests[0].headers["Authorization"] == "Bearer synthetic"


@pytest.mark.parametrize("query", ["limit=0", "limit=201", "limit=5&limit=6"])
def test_connector_invalid_pagination_never_reaches_domain(query):
    calls = []
    client, _, _ = make_client(lambda request: calls.append(request))
    with client:
        response = client.get("/v1/connectors?" + query, headers=headers())
    assert response.status_code == 400
    assert calls == []


def test_connector_foreign_tenant_header_never_reaches_domain():
    calls = []
    client, _, _ = make_client(lambda request: calls.append(request))
    with client:
        response = client.get(
            "/v1/connectors", headers={**headers(), "X-Tenant-ID": str(uuid4())}
        )
    assert response.status_code == 403
    assert calls == []


def test_connector_missing_upstream_fails_closed():
    client, _, _ = make_client(configured=False)
    with client:
        response = client.get("/v1/connectors", headers=headers())
    assert response.status_code == 503


def test_connector_malformed_upstream_response_is_unavailable():
    client, _, _ = make_client(
        lambda request: httpx.Response(200, json={"unexpected": True})
    )
    with client:
        response = client.get("/v1/connectors", headers=headers())
    assert response.status_code == 503


def test_connector_activation_has_no_facade_route():
    client, _, _ = make_client()
    with client:
        response = client.post("/v1/connectors/sample/activate", headers=headers())
    assert response.status_code == 404


@pytest.mark.parametrize(
    "value",
    [
        "https://example.invalid",
        "http://example.invalid",
        "http://connector-runtime@evil.invalid",
        "http://connector-runtime/api",
        "http://connector-runtime?redirect=evil",
    ],
)
def test_connector_configuration_cannot_send_credentials_to_arbitrary_origins(value):
    from app.core.config import Settings
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(connector_management_base_url=value)


def test_connector_missing_scope_never_reaches_domain():
    from app.security import AuthenticationError

    calls = []
    client, tokens, _ = make_client(lambda request: calls.append(request))
    tokens.verify.side_effect = AuthenticationError("required scope is missing")
    with client:
        response = client.get("/v1/connectors", headers=headers())
    assert response.status_code == 401
    assert calls == []


def test_connector_body_validation_precedes_domain_mutation():
    calls = []
    client, _, _ = make_client(lambda request: calls.append(request))
    with client:
        response = client.post(
            "/v1/connectors/install",
            headers={**headers(), "Idempotency-Key": "synthetic-idempotency"},
            json={"unexpected": True},
        )
    assert response.status_code == 400
    assert calls == []


def test_connector_http_error_is_sanitized():
    def fail(request):
        raise httpx.ConnectError("private upstream detail", request=request)

    client, _, _ = make_client(fail)
    with client:
        response = client.get("/v1/connectors", headers=headers())
    assert response.status_code == 503
    assert "private upstream detail" not in response.text
