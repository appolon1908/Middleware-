"""Fail-closed Keycloak JWT validation with bounded JWKS caching.

Two verifiers exist on purpose and share one identity source
(:class:`app.core.config.IdentitySettings`):

* :class:`~app.security.KeycloakJwtVerifier` — machine tokens on the Appolon
  control plane (requires ``sub``/``azp``/``jti``/``scope`` and the 300-second
  lifetime, async, cached JWKS).
* :class:`KeycloakValidator` — interactive and service tokens on the
  integration routes (authorized-party, role, scope and tenant-claim
  checks, synchronous).

Both reject any issuer, audience or JWKS authority other than the configured
one; neither ever falls back to a guessed authority. Both resolve keys
through :mod:`app.core.jwks` (one cached key set per authority).

Failures are classified so every route answers the same way
(:func:`auth_http_exception`):

* :class:`TokenInvalidError` — the token is not a valid token from the
  configured authority (signature, ``kid``, expiry, issuer, audience,
  lifetime, required or malformed claims): **401** with
  ``WWW-Authenticate: Bearer error="invalid_token"``;
* :class:`TokenAccessDenied` — a valid token without the authority the route
  requires (authorized party, role, scope, environment, business unit,
  campaign, tenant): **403** with ``error="insufficient_scope"``;
* :class:`IdentityUnavailableError` — the identity authority is not
  configured or its JWKS cannot be used: **503**, never a fallback.
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from typing import Any, NoReturn

import jwt
from fastapi import HTTPException

from app.core import jwks
from app.core.config import IdentitySettings
from app.core.identity_metrics import record_token_decision

MAX_SUBJECT_LENGTH = 255
REQUIRED_CLAIMS = ("exp", "iat", "iss", "aud", "sub")


class JWTAuthError(ValueError):
    """Base identity failure; ``reason`` is a fixed, metric-safe code."""

    status_code = 403
    reason = "invalid_token"

    def __init__(self, message: str, *, reason: str | None = None) -> None:
        super().__init__(message)
        if reason is not None:
            self.reason = reason


class TokenInvalidError(JWTAuthError):
    status_code = 401
    reason = "invalid_token"


class TokenAccessDenied(JWTAuthError):
    status_code = 403
    reason = "authorized_party"


class IdentityUnavailableError(JWTAuthError):
    status_code = 503
    reason = "not_configured"


_OUTCOMES = {401: "invalid", 403: "denied", 503: "unavailable"}


def auth_http_exception(exc: JWTAuthError, *, denied_detail: str) -> HTTPException:
    """The canonical HTTP answer for an identity failure.

    Details never echo token contents, claim values or upstream JWKS errors;
    ``denied_detail`` is the route family's fixed 403 message.
    """
    if isinstance(exc, IdentityUnavailableError):
        return HTTPException(503, "identity authority is unavailable")
    if isinstance(exc, TokenInvalidError):
        return HTTPException(
            401,
            "invalid bearer token",
            headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
        )
    return HTTPException(
        403,
        denied_detail,
        headers={"WWW-Authenticate": 'Bearer error="insufficient_scope"'},
    )


def raise_auth_http(exc: JWTAuthError, *, denied_detail: str) -> NoReturn:
    raise auth_http_exception(exc, denied_detail=denied_detail) from None


def identity_validator_kwargs(
    identity: IdentitySettings,
    *,
    authorized_parties: frozenset[str] | None = None,
    **requirements: Any,
) -> dict[str, Any]:
    """Constructor arguments binding a validator to the canonical identity.

    Fails closed when the identity is implicit: the integration routes only
    trust an explicitly configured issuer/audience/JWKS authority, never the
    derived default. ``requirements`` are the ``required_*`` fields.
    """
    if not identity.explicit:
        raise IdentityUnavailableError("Keycloak validation is not configured")
    return {
        "issuer": identity.issuer,
        "audience": identity.audience,
        "jwks_url": identity.jwks_url,
        "authorized_parties": (
            identity.authorized_parties if authorized_parties is None else authorized_parties
        ),
        "algorithms": identity.algorithms,
        "jwks_timeout_seconds": identity.jwks_timeout_seconds,
        "max_token_lifetime_seconds": identity.max_token_lifetime_seconds,
        **requirements,
    }


def _numeric_claim(claims: dict[str, Any], name: str) -> float:
    value = claims.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TokenInvalidError(f"{name} must be a numeric timestamp", reason="lifetime")
    numeric = float(value)
    if not math.isfinite(numeric):
        raise TokenInvalidError(f"{name} must be finite", reason="lifetime")
    return numeric


def _string_list(claims: dict[str, Any], name: str) -> set[str]:
    """An optional list-of-strings claim; any other shape is malformed."""
    value = claims.get(name, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise TokenInvalidError(f"{name} claim is malformed", reason="malformed_claims")
    return set(value)


def realm_roles(claims: dict[str, Any]) -> set[str]:
    realm = claims.get("realm_access", {})
    if not isinstance(realm, dict):
        raise TokenInvalidError("realm_access claim is malformed", reason="malformed_claims")
    return _string_list(realm, "roles")


def token_scopes(claims: dict[str, Any]) -> set[str]:
    scope = claims.get("scope", "")
    if not isinstance(scope, str):
        raise TokenInvalidError("scope claim is malformed", reason="malformed_claims")
    return set(scope.split())


@dataclass
class KeycloakValidator:
    issuer: str
    audience: str
    jwks_url: str
    authorized_parties: frozenset[str]
    required_roles: frozenset[str] = frozenset()
    required_scopes: frozenset[str] = frozenset()
    required_environment: str | None = None
    required_business_unit: str | None = None
    required_campaign: str | None = None
    algorithms: tuple[str, ...] = ("RS256",)
    jwks_timeout_seconds: float = 3
    max_token_lifetime_seconds: int | None = None

    @classmethod
    def from_identity(
        cls,
        identity: IdentitySettings,
        *,
        authorized_parties: frozenset[str] | None = None,
        required_roles: frozenset[str] = frozenset(),
        required_scopes: frozenset[str] = frozenset(),
        required_environment: str | None = None,
        required_business_unit: str | None = None,
        required_campaign: str | None = None,
    ) -> "KeycloakValidator":
        """Bind a validator to the canonical identity; fail closed if implicit."""
        return cls(
            **identity_validator_kwargs(
                identity,
                authorized_parties=authorized_parties,
                required_roles=required_roles,
                required_scopes=required_scopes,
                required_environment=required_environment,
                required_business_unit=required_business_unit,
                required_campaign=required_campaign,
            )
        )

    def validate(self, token: str) -> dict[str, Any]:
        try:
            claims = self._validate(token)
        except JWTAuthError as exc:
            record_token_decision(
                "keycloak_validator", _OUTCOMES.get(exc.status_code, "denied"), exc.reason
            )
            raise
        record_token_decision("keycloak_validator", "accepted", "ok")
        return claims

    async def validate_async(self, token: str) -> dict[str, Any]:
        """:meth:`validate` off the event loop (a JWKS fetch may block)."""
        return await asyncio.to_thread(self.validate, token)

    def _validate(self, token: str) -> dict[str, Any]:
        parties = frozenset(party for party in self.authorized_parties if party)
        if not all((self.issuer, self.audience, self.jwks_url, parties)):
            raise IdentityUnavailableError("Keycloak validation is not configured")
        if not isinstance(token, str) or not token:
            raise TokenInvalidError("bearer token is required")
        try:
            key = jwks.signing_key(
                jwks.jwks_client(self.jwks_url, timeout=self.jwks_timeout_seconds), token
            )
        except jwks.JwksUnavailableError as exc:
            raise IdentityUnavailableError(
                "identity key authority is unavailable", reason="jwks_unavailable"
            ) from exc
        except jwks.UnknownSigningKeyError as exc:
            raise TokenInvalidError("token validation failed", reason="unknown_key") from exc
        except Exception as exc:
            raise TokenInvalidError("token validation failed") from exc
        try:
            claims = jwt.decode(
                token,
                key.key,
                algorithms=list(self.algorithms),
                audience=self.audience,
                issuer=self.issuer,
                options={"require": list(REQUIRED_CLAIMS)},
            )
        except Exception as exc:
            raise TokenInvalidError("token validation failed") from exc
        if self.max_token_lifetime_seconds is not None:
            issued_at = _numeric_claim(claims, "iat")
            expires_at = _numeric_claim(claims, "exp")
            if not 0 < expires_at - issued_at <= self.max_token_lifetime_seconds:
                raise TokenInvalidError("token lifetime exceeds policy", reason="lifetime")
        subject = claims.get("sub")
        if (
            not isinstance(subject, str)
            or not subject.strip()
            or subject != subject.strip()
            or len(subject) > MAX_SUBJECT_LENGTH
        ):
            raise TokenInvalidError("stable subject required", reason="subject")
        if claims.get("azp") not in parties:
            raise TokenAccessDenied("authorized party denied", reason="authorized_party")
        if not self.required_roles.issubset(realm_roles(claims)):
            raise TokenAccessDenied("required role denied", reason="role")
        if not self.required_scopes.issubset(token_scopes(claims)):
            raise TokenAccessDenied("required scope denied", reason="scope")
        if (
            self.required_environment is not None
            and claims.get("environment") != self.required_environment
        ):
            raise TokenAccessDenied("environment denied", reason="environment")
        if (
            self.required_business_unit is not None
            and self.required_business_unit not in _string_list(claims, "business_units")
        ):
            raise TokenAccessDenied("business unit denied", reason="business_unit")
        if (
            self.required_campaign is not None
            and self.required_campaign not in _string_list(claims, "campaigns")
        ):
            raise TokenAccessDenied("campaign denied", reason="campaign")
        return claims
