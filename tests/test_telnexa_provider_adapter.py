from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import httpx
import pytest

from app.config import ConfigurationError
from app.telnexa_provider_adapter import (
    TelnexaProviderAdapterError,
    TelnexaSmsAdapter,
)
from app.temporal_workflows import CommandExecutionRequest

BASE_URL = "https://telnexa.internal.invalid"
API_KEY = "synthetic-telnexa-api-key-0123456789ab"
TENANT = "tenant-1"

ENV = {
    "TELNEXA_SMS_BASE_URL": BASE_URL,
    "TELNEXA_SMS_API_KEY": API_KEY,
}


class StubSettings:
    """Only the surface the adapter reads."""

    def __init__(self, *, app_env: str = "staging", sms_enabled: bool = True) -> None:
        self.app_env = app_env
        self.sms_delivery_enabled = sms_enabled


def execution_request(**overrides: Any) -> CommandExecutionRequest:
    identity = str(uuid4())
    payload: dict[str, Any] = {
        "message_id": str(uuid4()),
        "channel": "sms",
        "destination": "+15551234567",
        "sender": "Codestra",
        "content": "Your appointment is confirmed.",
        "encoding": "GSM-7",
        "characters": 29,
        "segments": 1,
        "category": "transactional",
        "client_reference": f"ref-{identity}",
        "scheduled_at": None,
        "billing_account_id": "billing-account-1",
        "campaign_id": None,
    }
    payload.update(overrides.pop("payload_overrides", {}))
    fields: dict[str, Any] = {
        "command_id": identity,
        "command_type": "sms.message.submit.v1",
        "command_version": "1.0",
        "target": "telnexa-sms",
        "tenant_id": TENANT,
        "requested_by": "codestra-communication",
        "correlation_id": f"correlation-{identity}",
        "idempotency_key": f"idempotency-{identity}",
        "capability": "SMS_DELIVERY",
        "payload": payload,
        "authenticated_client_id": "codestra-communication",
    }
    fields.update(overrides)
    return CommandExecutionRequest(**fields)


@pytest.fixture(autouse=True)
def _mock_httpx(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route every adapter request through the per-test MockTransport."""

    original = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        handler = _mock_httpx.handler  # type: ignore[attr-defined]
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


def set_handler(handler: Any) -> None:
    _mock_httpx.handler = handler  # type: ignore[attr-defined]


def accepted_body(message_id: str = "msg-1") -> dict[str, Any]:
    return {
        "message_id": message_id,
        "status": "accepted",
        "provider_message_id": "provider-abc",
        "simulated": False,
    }


@pytest.mark.asyncio
async def test_execute_submits_the_projected_body_and_security_headers() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content)
        return httpx.Response(202, json=accepted_body())

    set_handler(handler)
    command = execution_request()
    result = await TelnexaSmsAdapter(StubSettings(), env=ENV).execute(command)  # type: ignore[arg-type]

    assert result.status == "accepted"
    assert result.provider_operation_id == "provider-abc"
    assert seen["url"] == f"{BASE_URL}/api/v1/messages"
    assert seen["headers"]["x-api-key"] == API_KEY
    assert seen["headers"]["idempotency-key"] == command.idempotency_key
    assert seen["headers"]["x-correlation-id"] == command.correlation_id
    assert seen["headers"]["x-tenant-id"] == TENANT
    # Telnexa is the billing authority for segmentation, so Middleware must not
    # send its own encoding/segment counts.
    assert seen["body"] == {
        "billing_account_id": "billing-account-1",
        "destination": "+15551234567",
        "sender": "Codestra",
        "content": "Your appointment is confirmed.",
        "category": "transactional",
        "client_reference": command.payload["client_reference"],
    }
    assert "encoding" not in seen["body"]
    assert "segments" not in seen["body"]


@pytest.mark.asyncio
async def test_execute_is_refused_while_the_capability_is_closed() -> None:
    set_handler(lambda request: httpx.Response(202, json=accepted_body()))
    adapter = TelnexaSmsAdapter(
        StubSettings(sms_enabled=False),  # type: ignore[arg-type]
        env=ENV,
    )
    with pytest.raises(TelnexaProviderAdapterError, match="SMS delivery is disabled"):
        await adapter.execute(execution_request())


@pytest.mark.asyncio
async def test_execute_rejects_a_command_it_does_not_own() -> None:
    set_handler(lambda request: httpx.Response(202, json=accepted_body()))
    adapter = TelnexaSmsAdapter(StubSettings(), env=ENV)  # type: ignore[arg-type]
    with pytest.raises(TelnexaProviderAdapterError, match="does not own"):
        await adapter.execute(execution_request(target="odoo-19"))
    with pytest.raises(TelnexaProviderAdapterError, match="capability"):
        await adapter.execute(execution_request(capability="ODOO_WRITE"))
    with pytest.raises(TelnexaProviderAdapterError, match="unsupported"):
        await adapter.execute(execution_request(command_type="sms.message.send.v1"))


@pytest.mark.asyncio
async def test_execute_rejects_a_payload_that_violates_the_canonical_contract() -> None:
    set_handler(lambda request: httpx.Response(202, json=accepted_body()))
    adapter = TelnexaSmsAdapter(StubSettings(), env=ENV)  # type: ignore[arg-type]
    with pytest.raises(TelnexaProviderAdapterError, match="canonical contract"):
        await adapter.execute(
            execution_request(payload_overrides={"destination": "not-a-number"})
        )


@pytest.mark.asyncio
async def test_execute_refuses_to_forward_secret_bearing_payload_keys() -> None:
    set_handler(lambda request: httpx.Response(202, json=accepted_body()))
    adapter = TelnexaSmsAdapter(StubSettings(), env=ENV)  # type: ignore[arg-type]
    # additionalProperties:false means the contract rejects it before transport.
    with pytest.raises(TelnexaProviderAdapterError):
        await adapter.execute(
            execution_request(payload_overrides={"provider_token": "leaked"})
        )


@pytest.mark.asyncio
async def test_connection_failure_before_send_is_not_an_unknown_outcome() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    set_handler(handler)
    adapter = TelnexaSmsAdapter(StubSettings(), env=ENV)  # type: ignore[arg-type]
    with pytest.raises(TelnexaProviderAdapterError, match="before the submission"):
        await adapter.execute(execution_request())


@pytest.mark.asyncio
async def test_timeout_is_resolved_by_an_idempotent_replay_not_a_second_send() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ReadTimeout("timed out", request=request)
        return httpx.Response(202, json=accepted_body())

    set_handler(handler)
    command = execution_request()
    result = await TelnexaSmsAdapter(StubSettings(), env=ENV).execute(command)  # type: ignore[arg-type]

    assert result.status == "accepted"
    assert "unknown" in result.detail
    assert len(calls) == 2
    # The replay must be byte-identical and carry the same idempotency key, or
    # Telnexa would treat it as a different submission.
    assert calls[0].content == calls[1].content
    assert (
        calls[0].headers["idempotency-key"] == calls[1].headers["idempotency-key"]
    )


@pytest.mark.asyncio
async def test_gateway_5xx_is_reconciled_rather_than_failed() -> None:
    statuses = [502, 202]

    def handler(request: httpx.Request) -> httpx.Response:
        status = statuses.pop(0)
        if status == 202:
            return httpx.Response(202, json=accepted_body())
        return httpx.Response(status, json={"detail": "bad gateway"})

    set_handler(handler)
    result = await TelnexaSmsAdapter(StubSettings(), env=ENV).execute(  # type: ignore[arg-type]
        execution_request()
    )
    assert result.status == "accepted"


@pytest.mark.asyncio
async def test_idempotency_conflict_on_replay_stays_quarantined() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ReadTimeout("timed out", request=request)
        return httpx.Response(
            409, json={"detail": "idempotency_key_payload_mismatch"}
        )

    set_handler(handler)
    adapter = TelnexaSmsAdapter(StubSettings(), env=ENV)  # type: ignore[arg-type]
    with pytest.raises(TelnexaProviderAdapterError, match="already bound"):
        await adapter.execute(execution_request())


@pytest.mark.asyncio
async def test_provider_rejection_is_a_hard_failure() -> None:
    set_handler(
        lambda request: httpx.Response(
            409, json={"detail": "sender_not_approved"}
        )
    )
    adapter = TelnexaSmsAdapter(StubSettings(), env=ENV)  # type: ignore[arg-type]
    with pytest.raises(TelnexaProviderAdapterError, match="sender_not_approved"):
        await adapter.execute(execution_request())


@pytest.mark.asyncio
async def test_readback_reports_a_match_without_claiming_acceptance() -> None:
    set_handler(lambda request: httpx.Response(200, json=accepted_body()))
    result = await TelnexaSmsAdapter(StubSettings(), env=ENV).readback(  # type: ignore[arg-type]
        execution_request()
    )
    assert result.status == "matched"


@pytest.mark.asyncio
async def test_readback_reports_mismatch_for_an_unexpected_status() -> None:
    set_handler(lambda request: httpx.Response(500, json={"detail": "boom"}))
    result = await TelnexaSmsAdapter(StubSettings(), env=ENV).readback(  # type: ignore[arg-type]
        execution_request()
    )
    assert result.status == "mismatch"
    assert "500" in result.detail


@pytest.mark.asyncio
async def test_missing_configuration_is_refused() -> None:
    set_handler(lambda request: httpx.Response(202, json=accepted_body()))
    adapter = TelnexaSmsAdapter(StubSettings(), env={})  # type: ignore[arg-type]
    with pytest.raises(ConfigurationError, match="TELNEXA_SMS_BASE_URL"):
        await adapter.execute(execution_request())


@pytest.mark.asyncio
async def test_production_requires_https() -> None:
    set_handler(lambda request: httpx.Response(202, json=accepted_body()))
    adapter = TelnexaSmsAdapter(
        StubSettings(app_env="production"),  # type: ignore[arg-type]
        env={
            "TELNEXA_SMS_BASE_URL": "http://telnexa.internal.invalid",
            "TELNEXA_SMS_API_KEY": API_KEY,
        },
    )
    with pytest.raises(ConfigurationError, match="requires HTTPS"):
        await adapter.execute(execution_request())
