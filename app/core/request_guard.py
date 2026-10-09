"""The single request guard of the Middleware application.

One middleware, installed once by :func:`app.application.create_app`,
replaces the three guards the former applications each carried
(``app.main.control_request_guard``, ``app.entrypoints.runtime.request_controls``
and the Appolon ``observe_request``). It applies, in order:

1. body-size and content-length limits (413 / 400),
2. sales-intake content-type and size rules (415 / 413),
3. a per-client rate limit on signed write routes (429),
4. correlation and trace propagation (``request.state.correlation_id``),
5. the shared-secret bearer guard for ``/api/*`` and ``/v1/*`` routes that
   do **not** authenticate in their handler (401, or 503 when the process
   has no secret to check against),

and stamps ``X-Correlation-ID``, ``Cache-Control: no-store`` and
``Traceparent`` on every response.

Handler-authenticated routes are declared, never inferred: the explicit
tables in :mod:`app.core.route_policy`, the signed-ingress path sets below,
and the Appolon control-plane routers (every handler verifies a service
JWT through ``authenticated_tenant``). The guard never widens by prefix.
"""

from __future__ import annotations

import logging
import re
import time
from collections import OrderedDict, deque
from collections.abc import Callable, Iterable
from uuid import uuid4

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from app.api.v1.observability_sync import is_observability_sync_route
from app.core import route_policy
from app.core.auth import BearerAuthError, verify_bearer
from app.core.config import Settings
from app.monitoring.routes import is_monitoring_route
from app.observability import MiddlewareObservability, safe_correlation_id, safe_traceparent

logger = logging.getLogger("codestra.runtime")

CORRELATION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
MAX_RATE_IDENTITIES = 4096

# Routes whose handler verifies an HMAC signature or a service JWT itself.
SIGNED_WEBHOOK_PATHS = frozenset(
    {
        "/webhooks/vicidial/call-result/",
        "/webhooks/sms/inbound/",
        "/api/v1/events/vicidial",
        "/api/v1/events/telnexa",
        "/api/v1/events/klyrow",
        "/api/v1/automation/events",
        "/api/v2/telephony/canary",
        "/api/v1/n8n/executions",
        "/api/v1/n8n/executions/register",
        "/api/v1/n8n/acknowledgements",
        "/api/v1/n8n-runtime/results",
        "/api/v1/n8n-runtime/social-authorize",
        "/api/v1/lead-automation/results",
        "/api/v1/registry/resolve",
        "/api/v1/sales/scraper-results",
        "/api/v1/readiness/server-a/challenge",
        "/api/v1/integrations/breero/events",
    }
)
# Signed POST routes that are rate limited per client address.
RATE_LIMITED_SIGNED_WRITES = frozenset(
    {
        "/api/v1/events/vicidial",
        "/api/v1/events/telnexa",
        "/api/v1/events/klyrow",
        "/api/v2/telephony/canary",
        "/api/v1/n8n/executions",
        "/api/v1/n8n/executions/register",
        "/api/v1/n8n/acknowledgements",
        "/api/v1/sales/scraper-results",
        "/api/v1/readiness/server-a/challenge",
        "/api/v1/integrations/breero/events",
    }
)
SELF_AUTHENTICATED_PATHS = frozenset({"/v1/registry/search"})
SOCIAL_WEBHOOK_PATH = re.compile(r"^/api/v1/social/webhooks/(?:postly|hootsuite)$")
AI_CONSOLE_SELF_AUTHENTICATED_PATHS = (
    ("POST", re.compile(r"^/api/v1/ai/conversations$")),
    ("POST", re.compile(r"^/api/v1/ai/conversations/[0-9a-fA-F-]{36}/messages$")),
    ("GET", re.compile(r"^/api/v1/ai/jobs/[0-9a-fA-F-]{36}/stream$")),
    ("POST", re.compile(r"^/api/v1/ai/jobs/[0-9a-fA-F-]{36}/cancel$")),
    ("POST", re.compile(r"^/api/v1/ai/commands$")),
    ("GET", re.compile(r"^/api/v1/ai/commands/[0-9a-fA-F-]{36}$")),
    ("GET", re.compile(r"^/api/v1/ai/commands/[0-9a-fA-F-]{36}/result$")),
    ("POST", re.compile(r"^/api/v1/ai/commands/[0-9a-fA-F-]{36}/(?:cancel|approve|reject)$")),
    ("GET", re.compile(r"^/api/v1/ai/(?:capabilities|usage)$")),
    ("POST", re.compile(r"^/api/v1/ai/tts/stream$")),
)
N8N_TRANSITION_PATH = re.compile(r"^/api/v1/n8n/executions/[0-9a-fA-F-]{36}/transitions$")
RECORDING_EXPORTER_PATH = re.compile(
    r"^/api/v1/recordings(?:/reservations|/REC-[0-9a-f]{32}/(?:complete|failure))$"
)


def _is_ai_console_jwt_route(method: str, path: str) -> bool:
    return any(
        method == route_method and pattern.fullmatch(path)
        for route_method, pattern in AI_CONSOLE_SELF_AUTHENTICATED_PATHS
    )


CompiledRoute = tuple[frozenset[str], re.Pattern[str], str, APIRoute]


def _compiled_routes(routes: Iterable) -> list[CompiledRoute]:
    """``(methods, path regex, path template, route)`` in dispatch order."""
    compiled: list[CompiledRoute] = []

    def walk(routes, prefix: str = "") -> None:
        for route in routes:
            original = getattr(route, "original_router", None)
            if original is not None:
                context = getattr(route, "include_context", None)
                walk(original.routes, prefix + (getattr(context, "prefix", "") or ""))
                continue
            if isinstance(route, APIRoute):
                pattern = route.path_regex
                if prefix:
                    pattern = re.compile("^" + re.escape(prefix) + pattern.pattern.lstrip("^"))
                compiled.append(
                    (frozenset(route.methods or ()), pattern, prefix + route.path_format, route)
                )

    walk(routes)
    return compiled


class RequestGuard:
    """State and policy for the single guard middleware."""

    def __init__(
        self,
        settings: Settings,
        *,
        handler_authenticated_routers: Iterable[APIRouter] = (),
        telemetry: MiddlewareObservability | None = None,
        runtime_available: Callable[[], bool] | None = None,
    ) -> None:
        self.settings = settings
        self.telemetry = telemetry
        self.runtime_available = runtime_available or (lambda: True)
        self._handler_routes = _compiled_routes(
            route for router in handler_authenticated_routers for route in router.routes
        )
        self._handler_route_ids = frozenset(id(route) for *_rest, route in self._handler_routes)
        self._app: FastAPI | None = None
        self._served: tuple[int, list[CompiledRoute]] | None = None
        self._rate_windows: OrderedDict[str, deque[float]] = OrderedDict()

    def bind(self, app: FastAPI) -> None:
        """Resolve exemptions against the routes ``app`` actually dispatches."""
        self._app = app
        self._served = None

    def reset_rate_limits(self) -> None:
        """Forget every per-client window (tests and controlled resets)."""
        self._rate_windows.clear()

    # -- policy ---------------------------------------------------------
    def is_signed_write(self, method: str, path: str) -> bool:
        return method == "POST" and (
            path in RATE_LIMITED_SIGNED_WRITES or N8N_TRANSITION_PATH.fullmatch(path) is not None
        )

    def control_plane_route(self, method: str, path: str) -> bool:
        """A route of the handler-authenticated control-plane routers.

        These handlers verify a service JWT and enforce their own body limit
        (``max_request_body_bytes``) with the canonical error envelope.

        The decision follows the route the application will *dispatch*, not
        any route whose pattern happens to match: ``GET
        /api/v1/operations/{operation_id}`` (a JWT handler) also matches
        ``/api/v1/operations/dead-letters``, which an earlier, unauthenticated
        router serves. Exempting by pattern alone skipped the shared-secret
        check for that handler.
        """
        if not any(
            method in methods and pattern.fullmatch(path) is not None
            for methods, pattern, _template, _route in self._handler_routes
        ):
            return False
        if self._app is None:
            return True
        served = self._served_route(method, path)
        return served is not None and id(served) in self._handler_route_ids

    def control_plane_template(self, method: str, template: str) -> bool:
        """:meth:`control_plane_route` for an OpenAPI path template."""
        return any(
            method in methods and path_template == template
            for methods, _pattern, path_template, _route in self._handler_routes
        )

    def _served_route(self, method: str, path: str) -> APIRoute | None:
        """The first route in dispatch order that fully matches (Starlette's rule)."""
        assert self._app is not None
        routes = self._app.routes
        if self._served is None or self._served[0] != len(routes):
            self._served = (len(routes), _compiled_routes(routes))
        for methods, pattern, _template, route in self._served[1]:
            if method in methods and pattern.fullmatch(path) is not None:
                return route
        return None

    def handler_authenticated(self, method: str, path: str) -> bool:
        if path in SIGNED_WEBHOOK_PATHS or path in SELF_AUTHENTICATED_PATHS:
            return True
        if method == "POST" and (
            N8N_TRANSITION_PATH.fullmatch(path) or SOCIAL_WEBHOOK_PATH.fullmatch(path)
        ):
            return True
        if RECORDING_EXPORTER_PATH.fullmatch(path):
            return True
        if _is_ai_console_jwt_route(method, path):
            return True
        if route_policy.handler_authenticated(method, path):
            return True
        return self.control_plane_route(method, path)

    def guarded(self, request: Request) -> bool:
        path = request.url.path
        if not (path.startswith("/api/") or path.startswith("/v1/")):
            return False
        if self.handler_authenticated(request.method, path):
            return False
        return not (is_monitoring_route(request) or is_observability_sync_route(request))

    def rate_limited(self, request: Request) -> bool:
        identity = request.client.host if request.client else "unknown"
        now = time.monotonic()
        window = self._rate_windows.setdefault(identity, deque())
        self._rate_windows.move_to_end(identity)
        while len(self._rate_windows) > MAX_RATE_IDENTITIES:
            self._rate_windows.popitem(last=False)
        while window and window[0] < now - 60:
            window.popleft()
        limit = (
            self.settings.readiness_rate_limit_per_minute
            if request.url.path == "/api/v1/readiness/server-a/challenge"
            else self.settings.quarantine_rate_limit_per_minute
        )
        if len(window) >= limit:
            return True
        window.append(now)
        return False

    # -- middleware -----------------------------------------------------
    async def __call__(self, request: Request, call_next):
        settings = self.settings
        path = request.url.path
        # Control-plane handlers validate Content-Length and body size with
        # the canonical error envelope (read_limited_body); every other route
        # is bounded here.
        control_plane = self.control_plane_route(request.method, path)
        content_length = 0
        if not control_plane:
            try:
                content_length = int(request.headers.get("content-length", "0") or 0)
            except ValueError:
                return JSONResponse({"detail": "invalid content length"}, status_code=400)
            if content_length < 0:
                return JSONResponse({"detail": "invalid content length"}, status_code=400)

        correlation_id = safe_correlation_id(request.headers.get("X-Correlation-ID")) or str(uuid4())
        request.state.correlation_id = correlation_id
        client_correlation = request.headers.get("x-correlation-id", "").strip()
        request.state.client_correlation_id = (
            client_correlation if CORRELATION_RE.fullmatch(client_correlation) else None
        )
        gateway_request_id = request.headers.get("x-kong-request-id", "").strip()
        request.state.gateway_request_id = (
            gateway_request_id if CORRELATION_RE.fullmatch(gateway_request_id) else None
        )
        request.state.traceparent = safe_traceparent(request.headers.get("traceparent"))

        if request.method == "POST" and path.startswith("/api/v1/sales/"):
            content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if content_type != "application/json":
                return JSONResponse(
                    {
                        "code": "INVALID_CONTENT_TYPE",
                        "message": "application/json is required",
                        "correlation_id": correlation_id,
                        "retryable": False,
                    },
                    status_code=415,
                )
        if path.startswith("/api/v1/sales/") and content_length > settings.sales_lead_request_max_bytes:
            return JSONResponse(
                {
                    "code": "REQUEST_TOO_LARGE",
                    "message": "request exceeds the sales intake limit",
                    "correlation_id": correlation_id,
                    "retryable": False,
                },
                status_code=413,
            )
        if content_length > settings.request_max_bytes:
            return JSONResponse({"detail": "request too large"}, status_code=413)

        if self.is_signed_write(request.method, path) and self.rate_limited(request):
            return JSONResponse(
                {"detail": "rate limit exceeded"},
                status_code=429,
                headers={"Retry-After": "60", "X-Correlation-ID": correlation_id},
            )

        if self.guarded(request):
            try:
                verify_bearer(request.headers.get("Authorization", ""), settings.middleware_secret)
            except BearerAuthError:
                if not settings.middleware_secret:
                    return JSONResponse(
                        {"detail": "authentication unavailable"},
                        status_code=503,
                        headers={"X-Correlation-ID": correlation_id},
                    )
                return JSONResponse(
                    {"detail": "unauthorized"},
                    status_code=401,
                    headers={"X-Correlation-ID": correlation_id},
                )

        # Keep app.state.runtime current after a readiness-triggered rebuild;
        # routes that need the container obtain it through app.core.providers
        # and fail closed (503) themselves when it is absent.
        self.runtime_available()

        started = self.telemetry.start_request() if self.telemetry is not None else None
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
        finally:
            if self.telemetry is not None and started is not None:
                route = request.scope.get("route")
                template = getattr(route, "path", None)
                self.telemetry.finish_request(
                    started=started,
                    operation=template if isinstance(template, str) and template.startswith("/") else "unmatched",
                    method=request.method,
                    status_code=status_code,
                    correlation_id=correlation_id,
                    traceparent=request.state.traceparent,
                    intake_context=getattr(request.state, "intake_metrics", None),
                )
        response.headers["X-Correlation-ID"] = correlation_id
        response.headers["Cache-Control"] = "no-store"
        # A valid client traceparent is echoed, an invalid one is dropped, and
        # a request without one gets a fresh trace context.
        if request.state.traceparent is not None:
            response.headers["traceparent"] = request.state.traceparent
        elif "traceparent" not in request.headers:
            response.headers["traceparent"] = f"00-{uuid4().hex}-{uuid4().hex[:16]}-00"
        logger.info(
            "request_complete",
            extra={
                "correlation_id": correlation_id,
                "gateway_request_id": request.state.gateway_request_id,
                "result": f"{request.method} {path} {status_code}",
            },
        )
        return response


def install_request_guard(app: FastAPI, guard: RequestGuard) -> None:
    guard.bind(app)
    app.state.request_guard = guard
    app.middleware("http")(guard)
