"""Privilege comes only from validated tokens, never from caller-set headers."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.testclient import TestClient

from app.api.v1 import lead_reconciliation, operations, social
from app.core import integration_admin_auth, social_auth
from app.core.jwt_auth import JWTAuthError

BEARER = HTTPAuthorizationCredentials(scheme="Bearer", credentials="synthetic-token")


def _client(*routers) -> TestClient:
    app = FastAPI()
    for router in routers:
        app.include_router(router)
    return TestClient(app)


def _configure(monkeypatch, module, claims: dict | None) -> None:
    identity = SimpleNamespace(explicit=True, authorized_parties=("synthetic-client",))
    monkeypatch.setattr(module, "settings", SimpleNamespace(identity=identity))
    monkeypatch.setattr(module, "identity_validator_kwargs", lambda _identity: {})

    class Validator:
        def __init__(self, **_kwargs) -> None:
            pass

        def validate(self, _token: str) -> dict:
            if claims is None:
                raise JWTAuthError("invalid token")
            return claims

    monkeypatch.setattr(module, "KeycloakValidator", Validator)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "/api/v1/operations/maintenance/recover"),
        ("post", "/api/v1/operations/reconciliation"),
        ("post", "/api/v1/operations/dead-letters/00000000-0000-4000-8000-000000000001/replay"),
        ("get", "/api/v1/operations/dead-letters"),
        ("get", "/api/v1/operations/reliability"),
    ],
)
def test_role_header_alone_never_grants_integration_admin(method: str, path: str) -> None:
    response = getattr(_client(operations.router), method)(
        path, headers={"X-Codestra-Role": "integration_admin"}
    )
    assert response.status_code == 401


def test_role_header_alone_never_starts_lead_reconciliation() -> None:
    client = _client(lead_reconciliation.router)
    paths = [route.path for route in lead_reconciliation.router.routes]
    start = next(path for path in paths if path.endswith("/start"))
    response = client.post(start, headers={"X-Codestra-Role": "integration_admin"}, json={})
    assert response.status_code == 401


def test_permissions_header_alone_never_grants_social_access() -> None:
    client = _client(social.router)
    paths = [route.path for route in social.router.routes if "GET" in getattr(route, "methods", set())]
    response = client.get(paths[0], headers={"X-Codestra-Permissions": "social.admin"})
    assert response.status_code == 401


def test_integration_admin_requires_configured_identity_authority(monkeypatch) -> None:
    identity = SimpleNamespace(explicit=False, authorized_parties=())
    monkeypatch.setattr(integration_admin_auth, "settings", SimpleNamespace(identity=identity))
    with pytest.raises(HTTPException) as raised:
        integration_admin_auth.require_integration_admin(BEARER)
    assert raised.value.status_code == 503


@pytest.mark.parametrize(
    "claims",
    [
        None,
        {"sub": "user-1", "realm_access": {"roles": ["platform_operator"]}},
        {"sub": "user-1", "realm_access": {"roles": "integration_admin"}},
        {"sub": " ", "realm_access": {"roles": ["integration_admin"]}},
    ],
)
def test_integration_admin_denies_invalid_or_unprivileged_tokens(monkeypatch, claims) -> None:
    _configure(monkeypatch, integration_admin_auth, claims)
    with pytest.raises(HTTPException) as raised:
        integration_admin_auth.require_integration_admin(BEARER)
    assert raised.value.status_code == 403


def test_integration_admin_accepts_verified_admin_roles(monkeypatch) -> None:
    _configure(
        monkeypatch,
        integration_admin_auth,
        {"sub": "user-1", "realm_access": {"roles": ["integration_admin", "platform_admin"]}},
    )
    principal = integration_admin_auth.require_integration_admin(BEARER)
    assert principal.subject == "user-1"
    assert principal.role == "platform_admin"


def test_social_permissions_come_only_from_verified_scope(monkeypatch) -> None:
    _configure(
        monkeypatch,
        social_auth,
        {"sub": "user-1", "azp": "synthetic-client", "scope": "social.read", "tenant_ids": ["tenant-a"]},
    )
    principal = social_auth.require_social_principal(BEARER)
    assert principal.tenant_ids == frozenset({"tenant-a"})
    social_auth.require_social_permission(principal, "social.read")
    with pytest.raises(HTTPException) as raised:
        social_auth.require_social_permission(principal, "social.publish")
    assert raised.value.status_code == 403
