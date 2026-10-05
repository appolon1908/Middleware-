"""External event ingestion surface."""

from app.factory import create_service_app

from app.api.v1.events import router as events_router
from app.api.v1.publisher import router as publisher_router
from app.api.v1.readiness_challenge import router as readiness_router
from app.api.v1.breero import router as breero_router
from app.entrypoints.runtime import add_api_runtime, run_api


SERVICE = "middleware-event-gateway"
app = create_service_app(
    "Codestra Event Gateway",
    version="1.0.0",
    routes=[*events_router.routes, *publisher_router.routes, *readiness_router.routes, *breero_router.routes],
)
add_api_runtime(app, SERVICE)


if __name__ == "__main__":
    run_api(app, SERVICE)
