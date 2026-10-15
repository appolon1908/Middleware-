"""Private Controller API ASGI entrypoint.

This entrypoint is intentionally separate from ``app.main`` so controller
routes cannot appear on the public Middleware listener by importing a router.
Deployment must bind this app only to the approved private address.
"""

from app.factory import create_service_app
from fastapi.responses import JSONResponse

from app.api.v1.controller import controller, router
from app.core.config import settings
from app.core.request_guard import RequestGuard, install_request_guard

app = create_service_app("Codestra Private Controller", version="1.0.0")
install_request_guard(app, RequestGuard(settings))
app.include_router(router)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "exposure": "private-only"}


@app.get("/readyz", response_model=None)
async def readyz() -> dict[str, str] | JSONResponse:
    if not settings.controller_private_enabled:
        return JSONResponse({"status": "not-ready", "reason": "disabled"}, status_code=503)
    try:
        controller()
    except Exception:
        return JSONResponse(
            {"status": "not-ready", "reason": "signing-authority-unavailable"},
            status_code=503,
        )
    return {"status": "ready", "exposure": "private-only"}
