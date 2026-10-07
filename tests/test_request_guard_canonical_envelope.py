from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.request_guard import RequestGuard, install_request_guard


def _client(test_settings) -> TestClient:
    settings = test_settings.model_copy(update={"request_max_bytes": 16})
    app = FastAPI()

    @app.post("/v2/automation/probe")
    @app.post("/api/v1/probe")
    async def probe() -> dict[str, bool]:
        return {"ok": True}

    install_request_guard(app, RequestGuard(settings))
    return TestClient(app)


def test_canonical_api_guard_refusals_use_the_canonical_envelope(test_settings) -> None:
    client = _client(test_settings)
    headers = {"X-Correlation-ID": "guard-envelope-1", "Content-Type": "application/json"}
    oversized = client.post("/v2/automation/probe", content=b"x" * 64, headers=headers)
    assert oversized.status_code == 413
    assert oversized.headers["X-Correlation-ID"] == "guard-envelope-1"
    assert oversized.json() == {
        "error": {
            "code": "REQUEST_TOO_LARGE",
            "message": "request exceeds the configured body limit",
            "correlation_id": "guard-envelope-1",
            "retryable": False,
            "details": {},
        }
    }
    invalid = client.post("/v2/automation/probe", content=b"{}", headers={**headers, "Content-Length": "-1"})
    assert invalid.status_code == 400
    assert invalid.json()["error"]["code"] == "INVALID_CONTENT_LENGTH"


def test_legacy_routes_keep_their_guard_bodies(test_settings) -> None:
    client = _client(test_settings)
    response = client.post("/api/v1/probe", content=b"x" * 64, headers={"Content-Type": "application/json"})
    assert response.status_code == 413
    assert response.json() == {"detail": "request too large"}
