"""Canonical HTTP header authority for Middleware V3.

Public/control V3 APIs use the short transport names below.  X-Codestra-*
headers are reserved for explicitly versioned signed/internal protocols and
must not be introduced as aliases for canonical V3 request identity.

The identifier grammar and bounds declared here are the one authority the
request guard, the observability layer and every handler share: a request
identifier accepted by a handler is the identifier echoed on the response.
"""
from __future__ import annotations

import re

AUTHORIZATION = "Authorization"
TENANT_ID = "X-Tenant-ID"
CORRELATION_ID = "X-Correlation-ID"
CAUSATION_ID = "X-Causation-ID"
REQUEST_ID = "X-Request-ID"
ENVIRONMENT = "X-Environment"
IDEMPOTENCY_KEY = "Idempotency-Key"
TRACEPARENT = "traceparent"
TRACESTATE = "tracestate"

CANONICAL_V3_HEADERS = frozenset({
    AUTHORIZATION, TENANT_ID, CORRELATION_ID, CAUSATION_ID, REQUEST_ID,
    ENVIRONMENT, IDEMPOTENCY_KEY, TRACEPARENT, TRACESTATE,
})

# Request identifiers (correlation, causation, request) share one grammar and
# one bound; the public contract documents X-Correlation-ID as 1..180.
IDENTIFIER_MAX_LENGTH = 180
IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,179}$")
CORRELATION_ID_PATTERN = IDENTIFIER_PATTERN
REQUEST_ID_PATTERN = IDENTIFIER_PATTERN
CAUSATION_ID_PATTERN = IDENTIFIER_PATTERN
IDEMPOTENCY_KEY_MIN_LENGTH = 8
IDEMPOTENCY_KEY_MAX_LENGTH = 180
TENANT_ID_MAX_LENGTH = 128

# The deployment environment vocabulary of ``Settings.app_env``. A request
# that names another environment is refused before any handler runs.
ENVIRONMENTS = frozenset({"development", "test", "staging", "preproduction", "production"})

# These names are protocol fields covered by signatures or legacy event
# contracts. They are not aliases for canonical V3 API headers.
SIGNED_PROTOCOL_HEADERS = frozenset({
    "X-Codestra-Tenant-Id",
    "X-Codestra-Tenant",
    "X-Codestra-Correlation-ID",
    "X-Codestra-Request-ID",
    "X-Codestra-Causation-ID",
    "X-Codestra-Timestamp",
    "X-Codestra-Nonce",
    "X-Codestra-Body-SHA256",
    "X-Codestra-Content-SHA256",
    "X-Codestra-Signature",
})


def assert_canonical_v3_header(name: str) -> str:
    """Reject signed-protocol names when declaring ordinary V3 API headers."""
    if name in SIGNED_PROTOCOL_HEADERS:
        raise ValueError(f"{name} is reserved for a signed/internal protocol")
    if name not in CANONICAL_V3_HEADERS:
        raise ValueError(f"{name} is not a canonical Middleware V3 header")
    return name


def safe_identifier(value: str | None, pattern: re.Pattern[str] = IDENTIFIER_PATTERN) -> str | None:
    """The identifier itself when it matches the canonical grammar, else ``None``."""
    if value is None:
        return None
    candidate = value.strip()
    if not candidate or pattern.fullmatch(candidate) is None:
        return None
    return candidate
