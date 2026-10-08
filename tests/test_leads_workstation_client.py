from types import SimpleNamespace

import httpx
import pytest

from app.adapters.leads_workstation.client import (
    LeadsWorkstationClient,
    LeadsWorkstationConfig,
    LeadsWorkstationConfigurationError,
    LeadsWorkstationUpstreamError,
)


def _settings(url: str, token: str, timeout: float = 10.0):
    return SimpleNamespace(
        leads_workstation_url=url,
        leads_workstation_service_token=token,
        leads_workstation_timeout_seconds=timeout,
    )


def test_config_rejects_public_origin():
    with pytest.raises(LeadsWorkstationConfigurationError, match="internal host"):
        LeadsWorkstationConfig.from_settings(
            _settings("https://example.com", "secret")
        )


def test_config_accepts_private_service_name():
    config = LeadsWorkstationConfig.from_settings(
        _settings("http://leads-workstation:8765", "secret")
    )
    assert config.base_url == "http://leads-workstation:8765"
    assert config.service_token == "secret"


def test_config_requires_url_and_token():
    with pytest.raises(
        LeadsWorkstationConfigurationError,
        match="URL and service token",
    ):
        LeadsWorkstationConfig.from_settings(_settings("", ""))


@pytest.mark.asyncio
async def test_client_preserves_service_identity_and_request_context():
    observed = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed["request"] = request
        return httpx.Response(200, json={"lead_id": "lead-1"})

    client = LeadsWorkstationClient(
        LeadsWorkstationConfig(
            base_url="http://leads-workstation:8765",
            service_token="service-secret",
        ),
        transport=httpx.MockTransport(handler),
    )
    try:
        response = await client.request(
            "PATCH",
            "/api/v2/leads/lead-1",
            payload={"notes": "updated"},
            idempotency_key="IDM-1",
            request_id="REQ-1",
            correlation_id="COR-1",
            causation_id="CAU-1",
            traceparent="00-" + "1" * 32 + "-" + "2" * 16 + "-01",
            if_match='"4"',
        )
    finally:
        await client.aclose()

    assert response.status_code == 200
    request = observed["request"]
    assert request.headers["Authorization"] == "Bearer service-secret"
    assert request.headers["Idempotency-Key"] == "IDM-1"
    assert request.headers["X-Codestra-Request-ID"] == "REQ-1"
    assert request.headers["X-Codestra-Correlation-ID"] == "COR-1"
    assert request.headers["X-Codestra-Causation-ID"] == "CAU-1"
    assert request.headers["If-Match"] == '"4"'
    assert request.headers["traceparent"].startswith("00-")


@pytest.mark.asyncio
async def test_client_requires_idempotency_for_mutation():
    client = LeadsWorkstationClient(
        LeadsWorkstationConfig(
            base_url="http://leads-workstation:8765",
            service_token="service-secret",
        ),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"ok": True})
        ),
    )
    try:
        with pytest.raises(ValueError, match="Idempotency-Key"):
            await client.request(
                "POST",
                "/api/v2/leads",
                payload={"business_name": "Example"},
            )
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_upstream_server_failure_is_normalized():
    client = LeadsWorkstationClient(
        LeadsWorkstationConfig(
            base_url="http://leads-workstation:8765",
            service_token="service-secret",
        ),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(503, json={"error": "db unavailable"})
        ),
    )
    try:
        with pytest.raises(LeadsWorkstationUpstreamError) as exc:
            await client.request("GET", "/api/v2/leads")
    finally:
        await client.aclose()
    assert exc.value.status_code == 502
    assert exc.value.payload == {"error": "leads_workstation_unavailable"}
