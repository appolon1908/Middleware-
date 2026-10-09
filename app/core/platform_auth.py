"""Platform authority derived only from the verified original Keycloak token."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import settings
from app.core.jwt_auth import (
    JWTAuthError,
    KeycloakValidator,
    TokenAccessDenied,
    identity_validator_kwargs,
    raise_auth_http,
    realm_roles,
)

BEARER = HTTPBearer(auto_error=False)
PLATFORM_ROLES = frozenset({"platform_admin", "platform_reviewer", "platform_operator"})


@dataclass(frozen=True)
class PlatformPrincipal:
    subject: str
    role: str


def require_platform_scope(
    scope: str, allowed_roles: frozenset[str] = PLATFORM_ROLES,
) -> Callable[..., PlatformPrincipal]:
    """Each endpoint declares a scope; headers never supply identity or privilege.

    This synchronous dependency keeps JWKS I/O off the ASGI event loop. Missing
    issuer/audience/client configuration is unavailable, not shared-secret auth.
    """
    def authorize(
        credential: HTTPAuthorizationCredentials | None = Depends(BEARER),
    ) -> PlatformPrincipal:
        if credential is None or credential.scheme.lower() != "bearer":
            raise HTTPException(401, "verified platform bearer required", headers={"WWW-Authenticate": "Bearer"})
        identity = settings.identity
        if not identity.explicit or not identity.authorized_parties:
            raise HTTPException(503, "platform identity authority is not configured")
        try:
            validator = KeycloakValidator(
                **identity_validator_kwargs(identity, required_scopes=frozenset({scope}))
            )
            # The validator enforces signature, issuer, audience, lifetime,
            # a stable ``sub``, the authorized party, the scope and the
            # claim shapes; only the role decision is local.
            claims = validator.validate(credential.credentials)
            subject = claims["sub"]
            eligible = allowed_roles.intersection(realm_roles(claims))
            if not eligible:
                raise TokenAccessDenied("platform role denied", reason="role")
        except JWTAuthError as exc:
            # Never expose credentials, upstream JWKS details or claim contents.
            raise_auth_http(exc, denied_detail="platform authority denied")
        role = "platform_admin" if "platform_admin" in eligible else sorted(eligible)[0]
        return PlatformPrincipal(subject=subject, role=role)

    return authorize
