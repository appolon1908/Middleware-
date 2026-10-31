"""Runtime contract of the M1-F01 application factory (Section 1).

Proves the factory's observable behaviour on the deployed integration
profile: one construction authority, rejected unknown profiles, a single
request guard, correlation on every response and error, health/version,
production documentation off, fail-closed production configuration, and the
:8095 runtime port.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from app import application, factory
from app.application import AppProfile, create_app
from app.core.config import ConfigurationError, Settings
from app.core.request_guard import RequestGuard
from app.factory import create_service_app

ROOT = Path(__file__).resolve().parents[1]
UNKNOWN_OPERATION = "/platform/v1/operations/3fa85f64-5717-4562-b3fc-2c963f66afa6"


def _settings(**overrides: object) -> Settings:
    settings = Settings.from_env({"APP_ENV": "test", "ALLOW_IN_MEMORY_STORAGE": "true"})
    return settings.model_copy(update=overrides) if overrides else settings


@pytest.fixture
def client() -> TestClient:
    with TestClient(create_app(settings=_settings(), profile=AppProfile.INTEGRATION)) as test_client:
        yield test_client


def test_factory_create_app_is_the_composition_authority() -> None:
    assert factory.create_app is application.create_app
    assert factory.AppProfile is application.AppProfile


def test_unsupported_profile_is_rejected() -> None:
    with pytest.raises(ValueError):
        create_app(settings=_settings(), profile="bogus")


def test_request_guard_is_installed_once_as_the_outermost_middleware() -> None:
    app = create_app(settings=_settings(), profile=AppProfile.INTEGRATION)
    guards = [entry for entry in app.user_middleware if isinstance(entry.kwargs.get("dispatch"), RequestGuard)]
    assert len(guards) == 1
    # user_middleware[0] wraps every other middleware and the router.
    assert app.user_middleware[0] is guards[0]


def test_incoming_correlation_id_is_preserved(client: TestClient) -> None:
    response = client.get("/health/live", headers={"X-Correlation-ID": "corr-section1-a"})
    assert response.status_code == 200
    assert response.headers["X-Correlation-ID"] == "corr-section1-a"


def test_missing_correlation_id_is_generated(client: TestClient) -> None:
    response = client.get("/health/live")
    assert response.headers["X-Correlation-ID"]


def test_authentication_failure_carries_the_correlation_id(client: TestClient) -> None:
    response = client.get(UNKNOWN_OPERATION, headers={"X-Correlation-ID": "corr-section1-b"})
    assert response.status_code == 401
    assert response.headers["X-Correlation-ID"] == "corr-section1-b"
    error = response.json()["error"]
    assert error["code"] == "authentication_failed"
    assert error["correlation_id"] == "corr-section1-b"
    assert error["retryable"] is False


def test_unmatched_route_uses_the_canonical_envelope(client: TestClient) -> None:
    response = client.get("/no/such/route", headers={"X-Correlation-ID": "corr-section1-c"})
    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "code": "not_found",
            "message": "no route matches the request",
            "correlation_id": "corr-section1-c",
            "retryable": False,
            "details": {},
        }
    }


def test_health_and_version_are_served(client: TestClient) -> None:
    assert client.get("/health/live").json()["status"] == "ok"
    assert client.get("/health/ready").status_code == 200
    version = client.get("/version").json()
    assert version["service"] == "middleware-integration-api"
    assert {"version", "environment", "git_sha"} <= set(version)
    serialized = repr(version).lower()
    for secret in ("password", "secret", "token", "private_key"):
        assert secret not in serialized


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_documentation_is_off_outside_development(environment: str) -> None:
    app = create_app(settings=_settings(app_env=environment), profile=AppProfile.INTEGRATION)
    assert app.docs_url is None and app.redoc_url is None


def test_service_apps_publish_no_documentation_unless_asked() -> None:
    paths = {getattr(route, "path", "") for route in create_service_app("probe").routes}
    assert "/docs" not in paths and "/redoc" not in paths
    opted_in = {getattr(route, "path", "") for route in create_service_app("probe", docs_url="/docs").routes}
    assert "/docs" in opted_in


def test_production_without_identity_configuration_fails_closed() -> None:
    with pytest.raises(ConfigurationError):
        Settings.from_env({"APP_ENV": "production", "KEYCLOAK_ISSUER": ""}).validate_safety()


def test_api_runtime_listens_on_8095_only() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert set(re.findall(r"^EXPOSE (\d+)$", dockerfile, re.MULTILINE)) == {"8095"}
    assert 'CMD ["/opt/venv/bin/python", "-m", "app.entrypoints.integration_api"]' in dockerfile
    compose = yaml.safe_load((ROOT / "deploy" / "compose.runtime.yaml").read_text(encoding="utf-8"))
    declared: set[str] = set()
    for service in compose["services"].values():
        declared.update(str(port) for port in service.get("expose", ()))
        environment = service.get("environment") or {}
        if isinstance(environment, dict) and "PORT" in environment:
            declared.add(str(environment["PORT"]))
    assert declared == {"8095"}


def test_errors_negotiate_rfc9457_problem_details(client: TestClient) -> None:
    response = client.get(
        UNKNOWN_OPERATION,
        headers={"X-Correlation-ID": "corr-section1-d", "Accept": "application/problem+json"},
    )
    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.headers["X-Correlation-ID"] == "corr-section1-d"
    assert response.json() == {
        "type": "https://middleware.codestra.co/problems/authentication_failed",
        "title": "Unauthorized",
        "status": 401,
        "detail": "Authorization must be a Bearer token",
        "instance": UNKNOWN_OPERATION,
        "error_code": "authentication_failed",
        "correlation_id": "corr-section1-d",
    }


def test_unmatched_route_negotiates_problem_details(client: TestClient) -> None:
    response = client.get("/no/such/route", headers={"Accept": "application/problem+json"})
    document = response.json()
    assert response.status_code == 404 and document["status"] == 404
    assert document["error_code"] == "not_found" and document["title"] == "Not Found"
    assert document["correlation_id"] == response.headers["X-Correlation-ID"]
