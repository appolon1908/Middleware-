"""RFC 9457 Problem Details by content negotiation.

The canonical error envelope (``{"error": {code, message, correlation_id,
retryable, details}}``) stays the default contract. A client that sends
``Accept: application/problem+json`` receives the same error as a Problem
Details document from the canonical error authority
(``app.appolon_routes.error_response``); the request guard stays the single,
non-rewriting HTTP middleware.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import Any

from starlette.types import Scope

PROBLEM_MEDIA_TYPE = "application/problem+json"
PROBLEM_TYPE_BASE = "https://middleware.codestra.co/problems/"


def wants_problem(scope: Scope) -> bool:
    for name, value in scope.get("headers", ()):
        if name == b"accept" and PROBLEM_MEDIA_TYPE.encode() in value.lower():
            return True
    return False


def problem_document(status: int, payload: Any, *, instance: str, correlation_id: str | None) -> dict[str, Any] | None:
    """Translate a JSON error body into a Problem Details document."""
    if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
        error = payload["error"]
        code = str(error.get("code") or "error")
        detail = str(error.get("message") or "")
        correlation_id = str(error.get("correlation_id") or correlation_id or "")
    elif isinstance(payload, dict) and "detail" in payload:
        raw = payload["detail"]
        code = "invalid_request" if status == 422 else _status_code_name(status)
        detail = raw if isinstance(raw, str) else "request validation failed"
    else:
        return None
    try:
        title = HTTPStatus(status).phrase
    except ValueError:
        title = "Error"
    document: dict[str, Any] = {
        "type": PROBLEM_TYPE_BASE + code,
        "title": title,
        "status": status,
        "detail": detail,
        "instance": instance,
        "error_code": code,
    }
    if correlation_id:
        document["correlation_id"] = correlation_id
    return document


def _status_code_name(status: int) -> str:
    try:
        return HTTPStatus(status).name.lower()
    except ValueError:
        return "error"
