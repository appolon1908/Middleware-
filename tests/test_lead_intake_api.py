from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from app.main import create_app
from app.replay import MemoryReplayGuard
from app.runtime import Runtime
from app.storage import MemoryInboxStore


class IntakeTokenVerifier:
    async def verify(
        self,
        authorization: str,
        *,
        expected_client_id: str,
        required_scope: str,
    ) -> dict[str, Any]:
        from app.security import AuthenticationError

        assert expected_client_id == "sdk-intake"
        assert required_scope == "leads.write"
        if authorization != "Bearer intake-token":
            raise AuthenticationError("invalid intake token")
        return {
            "azp": "sdk-intake",
            "scope": "leads.write",
            "aud": "middleware-api",
            "tenant_id": "tenant-1",
            "sub": "sdk-intake",
        }

    async def ready(self) -> bool:
        return True


def lead_payload(**updates: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "tenantId": "tenant-1",
        "siteId": "landing-001",
        "submittedAt": "2026-08-29T19:45:00+00:00",
        "source": "landing_page",
        "formId": "credit-repair-lead",
        "campaignId": "campaign-001",
        "name": "Test Lead",
        "email": "lead@example.com",
        "phone": "+18095550123",
        "message": "Please contact me.",
        "consent": {
            "marketing": True,
            "sms": True,
            "email": True,
            "privacyPolicyVersion": "2026-08-29",
        },
        "attribution": {
            "source": "google",
            "medium": "cpc",
            "campaign": "credit-repair",
            "landingPage": "https://example.test/credit-repair",
        },
    }
    value.update(updates)
    return value


def headers(**updates: str) -> dict[str, str]:
    value = {
        "Authorization": "Bearer intake-token",
        "X-Tenant-ID": "tenant-1",
        "X-Correlation-ID": "lead-correlation-001",
        "Idempotency-Key": "lead-idempotency-001",
        "Content-Type": "application/json",
    }
    value.update(updates)
    return value


def client_for(test_settings) -> TestClient:
    runtime = Runtime(
        settings=test_settings,
        inbox=MemoryInboxStore(),
        replay=MemoryReplayGuard(),
        tokens=IntakeTokenVerifier(),
    )
    return TestClient(create_app(settings=test_settings, runtime=runtime))


def test_lead_intake_accepts_and_deduplicates(test_settings) -> None:
    with client_for(test_settings) as client:
        first = client.post("/v1/intake/leads", json=lead_payload(), headers=headers())
        assert first.status_code == 202, first.text
        assert first.json()["status"] == "accepted"
        assert first.json()["duplicate"] is False
        assert first.json()["tenant_id"] == "tenant-1"
        assert first.headers["x-correlation-id"] == "lead-correlation-001"

        duplicate = client.post("/v1/intake/leads", json=lead_payload(), headers=headers())
        assert duplicate.status_code == 200, duplicate.text
        assert duplicate.json()["status"] == "duplicate"
        assert duplicate.json()["duplicate"] is True
        assert duplicate.json()["event_id"] == first.json()["event_id"]


def test_lead_intake_rejects_idempotency_payload_conflict(test_settings) -> None:
    with client_for(test_settings) as client:
        first = client.post("/v1/intake/leads", json=lead_payload(), headers=headers())
        assert first.status_code == 202, first.text

        conflict = client.post(
            "/v1/intake/leads",
            json=lead_payload(message="Changed payload under the same idempotency key"),
            headers=headers(),
        )
        assert conflict.status_code == 409, conflict.text
        assert conflict.json()["error"]["code"] == "idempotency_conflict"


def test_lead_intake_enforces_auth_tenant_and_required_headers(test_settings) -> None:
    with client_for(test_settings) as client:
        bad_token = client.post(
            "/v1/intake/leads",
            json=lead_payload(),
            headers=headers(Authorization="Bearer wrong-token"),
        )
        assert bad_token.status_code == 401

        wrong_header_tenant = client.post(
            "/v1/intake/leads",
            json=lead_payload(),
            headers=headers(**{"X-Tenant-ID": "tenant-2"}),
        )
        assert wrong_header_tenant.status_code == 403

        wrong_claim_tenant = client.post(
            "/v1/intake/leads",
            json=lead_payload(tenantId="tenant-2"),
            headers=headers(**{"X-Tenant-ID": "tenant-2"}),
        )
        assert wrong_claim_tenant.status_code == 403

        missing_idempotency = headers()
        missing_idempotency.pop("Idempotency-Key")
        missing = client.post(
            "/v1/intake/leads",
            json=lead_payload(),
            headers=missing_idempotency,
        )
        assert missing.status_code == 400
        assert missing.json()["error"]["code"] == "invalid_request"


def test_lead_intake_rejects_invalid_contract(test_settings) -> None:
    with client_for(test_settings) as client:
        invalid = client.post(
            "/v1/intake/leads",
            json={**lead_payload(), "unexpected": True},
            headers=headers(),
        )
        assert invalid.status_code == 400
        assert invalid.json()["error"]["code"] == "invalid_request"

        invalid_source = client.post(
            "/v1/intake/leads",
            json=lead_payload(source="unknown-source"),
            headers=headers(**{"Idempotency-Key": "lead-idempotency-002"}),
        )
        assert invalid_source.status_code == 400

        missing_submitted_at = lead_payload()
        missing_submitted_at.pop("submittedAt")
        missing_timestamp = client.post(
            "/v1/intake/leads",
            json=missing_submitted_at,
            headers=headers(**{"Idempotency-Key": "lead-idempotency-003"}),
        )
        assert missing_timestamp.status_code == 400


def test_lead_intake_rejects_oversized_body(test_settings) -> None:
    with client_for(test_settings) as client:
        oversized = lead_payload(message="x" * (test_settings.max_request_body_bytes + 1))
        response = client.post(
            "/v1/intake/leads",
            json=oversized,
            headers=headers(**{"Idempotency-Key": "lead-idempotency-oversized"}),
        )
        assert response.status_code == 413
        assert response.json()["error"]["code"] == "payload_too_large"
