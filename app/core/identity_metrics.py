"""Bounded-cardinality identity decision metrics.

Labels are drawn only from fixed vocabularies (verifier names and the
``reason`` codes of :mod:`app.core.jwt_auth`); subjects, clients, tenants and
token contents never become label values.
"""

from __future__ import annotations

from prometheus_client import Counter

TOKEN_DECISIONS = Counter(
    "codestra_identity_token_decisions_total",
    "Bearer token verification decisions by verifier, outcome and reason",
    ("verifier", "outcome", "reason"),
)

VERIFIERS = frozenset({"keycloak_validator", "control_plane", "email"})
OUTCOMES = frozenset({"accepted", "invalid", "denied", "unavailable"})
REASONS = frozenset(
    {
        "ok",
        "not_configured",
        "jwks_unavailable",
        "unknown_key",
        "invalid_token",
        "lifetime",
        "subject",
        "authorized_party",
        "role",
        "scope",
        "environment",
        "business_unit",
        "campaign",
        "tenant",
        "malformed_claims",
    }
)


def record_token_decision(verifier: str, outcome: str, reason: str) -> None:
    if verifier not in VERIFIERS or outcome not in OUTCOMES or reason not in REASONS:
        raise ValueError("identity metric label outside the fixed vocabulary")
    TOKEN_DECISIONS.labels(verifier, outcome, reason).inc()
