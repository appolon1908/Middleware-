"""Monitoring authority comes from the original verified JWT, never headers."""

from __future__ import annotations

from dataclasses import dataclass
import re

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import settings
from app.core.jwt_auth import (
    JWTAuthError,
    KeycloakValidator,
    TokenAccessDenied,
    TokenInvalidError,
    identity_validator_kwargs,
    raise_auth_http,
    realm_roles,
)

bearer = HTTPBearer(auto_error=False)
ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
READ_ROLES = frozenset({"platform_admin", "platform_operator", "platform_reviewer"})


@dataclass(frozen=True)
class Principal:
    subject: str
    tenant: str
    roles: frozenset[str]
    campaigns: frozenset[str]
    services: frozenset[str]
    client: str

    def check_campaign(self, campaign: str | None):
        if "campaign_supervisor" in self.roles and not self.roles.intersection(
            READ_ROLES
        ):
            if campaign is not None and campaign not in self.campaigns:
                raise HTTPException(403, "campaign authority denied")


def require(scope: str, roles: frozenset[str] = READ_ROLES):
    def authorize(
        credential: HTTPAuthorizationCredentials | None = Depends(bearer),
    ) -> Principal:
        if credential is None or credential.scheme.lower() != "bearer":
            raise HTTPException(
                401,
                "verified monitoring bearer required",
                headers={"WWW-Authenticate": "Bearer"},
            )
        identity = settings.identity
        if not identity.explicit or not identity.authorized_parties:
            raise HTTPException(503, "monitoring identity authority is not configured")
        try:
            claims = KeycloakValidator(
                **identity_validator_kwargs(identity, required_scopes=frozenset({scope}))
            ).validate(credential.credentials)
            tenant = claims.get("tenant_id")
            raw_roles = realm_roles(claims)
            campaigns, services = (
                claims.get("campaigns", []),
                claims.get("services", []),
            )
            if not isinstance(tenant, str) or not ID.fullmatch(tenant):
                raise TokenAccessDenied("tenant claim required", reason="tenant")
            for collection in (raw_roles, campaigns, services):
                if not isinstance(collection, (list, set)) or not all(
                    isinstance(v, str) and ID.fullmatch(v) for v in collection
                ):
                    raise TokenInvalidError("claims malformed", reason="malformed_claims")
            if not roles.intersection(raw_roles):
                raise TokenAccessDenied("monitoring role denied", reason="role")
        except JWTAuthError as exc:
            raise_auth_http(exc, denied_detail="monitoring authority denied")
        return Principal(
            claims["sub"],
            tenant,
            frozenset(raw_roles),
            frozenset(campaigns),
            frozenset(services),
            claims["azp"],
        )

    return authorize
