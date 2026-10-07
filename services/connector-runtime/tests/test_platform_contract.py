"""Control route authorization and redacted error contracts without providers."""

from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from codestra_connector_runtime.api.app import create_app
from codestra_connector_runtime.api.auth import Principal, principal_dependency
from codestra_connector_runtime.api.config import RuntimeSettings


def settings():
    return RuntimeSettings(
        database_url="postgresql+psycopg://unused@127.0.0.1/unused",
        cursor_hmac_key="x" * 48,
        body_encryption_key_file=Path("/tmp/unused"),
    )


def client(scopes):
    app = create_app()
    app.state.settings = settings()

    async def principal():
        return Principal(
            "test", "issuer", "client", frozenset({uuid4()}), frozenset(scopes), {}
        )

    app.dependency_overrides[principal_dependency] = principal
    return TestClient(app)


def test_platform_routes_have_the_same_scope_boundaries():
    test_client = client([])
    for path in [
        "",
        "/example/manifest",
        "/example/health",
        "/example/status",
        "/example/capabilities",
    ]:
        response = test_client.get("/platform/v1/connectors" + path)
        assert response.status_code == 403
        assert response.json()["code"] == "INSUFFICIENT_SCOPE"


def test_platform_install_is_disabled_by_default():
    response = client(["connector.install.request"]).post(
        "/platform/v1/connectors/install",
        json={"manifest": {}, "expected_manifest_digest": "sha256:" + "a" * 64},
    )
    assert response.status_code == 403
    assert response.json()["code"] == "CAPABILITY_DISABLED"


def test_validation_problems_never_echo_untrusted_input():
    response = client(["connector.install.request"]).post(
        "/platform/v1/connectors/install",
        json={"manifest": {}, "expected_manifest_digest": "secret-value-do-not-echo"},
    )
    assert response.status_code == 422
    assert "secret-value-do-not-echo" not in response.text


def test_platform_management_body_limit_is_enforced():
    response = client(["connector.install.request"]).post(
        "/platform/v1/connectors/install",
        content=b"x" * 1_048_577,
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 413


def test_invalid_trace_context_is_rejected_without_echoing_input():
    response = client([]).get(
        "/platform/v1/connectors", headers={"traceparent": "secret-do-not-echo"}
    )
    assert response.status_code == 400
    assert response.json()["code"] == "TRACE_CONTEXT_INVALID"
    assert "secret-do-not-echo" not in response.text


def test_versioned_openapi_has_unique_platform_operation_ids():
    import yaml

    root = Path(__file__).resolve().parents[3]
    contract = yaml.safe_load(
        (root / "contracts/connectors/connector-management-api.v1.yaml").read_text()
    )
    operation_ids = []
    for path, item in contract["paths"].items():
        for method in ("get", "post", "patch", "delete"):
            if method in item:
                operation_ids.append(item[method]["operationId"])
        if path.startswith("/v1/connectors") and not path.endswith(
            ("/status", "/capabilities")
        ):
            assert "/platform" + path in contract["paths"]
    assert len(operation_ids) == len(set(operation_ids))


def test_generated_openapi_matches_the_connector_standard_version():
    app = create_app()
    assert app.openapi()["openapi"] == "3.1.1"
