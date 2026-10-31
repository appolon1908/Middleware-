"""Narrow authenticated webphone session API."""

from app.factory import create_service_app

from app.api.v1.webphone import router
from app.api.v1.agent_realtime import router as realtime_router
from app.entrypoints.runtime import add_api_runtime, run_api

SERVICE = "codestra-webphone-session-issuer"
app = create_service_app(SERVICE, routes=[*router.routes, *realtime_router.routes])
add_api_runtime(app, SERVICE)

if __name__ == "__main__":
    run_api(app, SERVICE)
