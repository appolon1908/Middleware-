"""Narrow provisioning saga API; production mutations are kill-switched."""

from app.factory import create_service_app

from app.api.v1.telephony import router
from app.entrypoints.runtime import add_api_runtime, run_api

SERVICE = "codestra-telephony-provisioning"
app = create_service_app(
    SERVICE,
    routes=[
        route
        for route in router.routes
        if "/v1/telephony/provisioning" in getattr(route, "path", "")
    ],
)
add_api_runtime(app, SERVICE)

if __name__ == "__main__":
    run_api(app, SERVICE)
