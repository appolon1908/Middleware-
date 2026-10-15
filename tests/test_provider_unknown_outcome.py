"""A provider error raised after the send must not look like a safe retry.

The registered bridges map pre-send failures to TRANSIENT (the kernel may
retry) and post-send failures to UNKNOWN (the kernel reads back instead).
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from app.commands import CommandEnvelope
from app.core.config import Settings
from app.klyrow_email_adapter import KlyrowEmailAdapterError, KlyrowEmailUnknownOutcomeError
from app.odoo_provider_adapter import OdooProviderAdapterError, OdooUnknownOutcomeError
from app.platform.adapter import AdapterContext, ErrorClass, Outcome
from app.platform.adapters.providers import provider_adapters
from app.telnexa_provider_adapter import TelnexaProviderAdapterError, TelnexaUnknownOutcomeError

ENV = {
    "KLYROW_EMAIL_API_BASE_URL": "https://klyrow-email.internal.invalid",
    "KLYROW_EMAIL_MTLS_CA_FILE": "/run/secrets/ca.pem",
    "KLYROW_EMAIL_MTLS_CERT_FILE": "/run/secrets/cert.pem",
    "KLYROW_EMAIL_MTLS_KEY_FILE": "/run/secrets/key.pem",
    "TELNEXA_SMS_BASE_URL": "https://telnexa.internal.invalid",
    "TELNEXA_SMS_API_KEY": "synthetic-fixture-key",
    "ODOO_INTEGRATION_BASE_URL": "https://odoo.internal.invalid",
    "ODOO_INBOUND_HMAC_SECRET": "synthetic-fixture-secret",
}

CASES = [
    ("odoo-19", "crm.lead.upsert", "ODOO_WRITE", OdooProviderAdapterError, OdooUnknownOutcomeError),
    ("klyrow-email", "email.message.send.v1", "EMAIL_DELIVERY", KlyrowEmailAdapterError, KlyrowEmailUnknownOutcomeError),
    ("telnexa-sms", "sms.message.submit.v1", "SMS_DELIVERY", TelnexaProviderAdapterError, TelnexaUnknownOutcomeError),
]


class _Raising:
    def __init__(self, error: BaseException) -> None:
        self.error = error

    async def execute(self, request: Any) -> Any:
        raise self.error

    async def readback(self, request: Any) -> Any:
        raise AssertionError("execute must not read back here")


def _registered(monkeypatch: pytest.MonkeyPatch, adapter_id: str) -> Any:
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    settings = Settings.from_env({"APP_ENV": "test", "ALLOW_IN_MEMORY_STORAGE": "true"})
    adapters = {adapter.adapter_id: adapter for adapter in provider_adapters(settings, http=None)}
    assert adapter_id in adapters
    return adapters[adapter_id]


def _command(command_type: str, target: str, capability: str) -> CommandEnvelope:
    # The bridge maps the legacy adapter's exception and never reads the
    # payload, so the envelope skips the per-command payload contract.
    return CommandEnvelope.model_construct(
        command_id=uuid4(),
        command_type=command_type,
        command_version="1.0",
        target=target,
        tenant_id="TEST_SYN",
        requested_by="user-1",
        correlation_id="corr-unknown-1",
        idempotency_key="idem-unknown-1",
        capability=capability,
        payload={},
    )


def _context(command: CommandEnvelope) -> AdapterContext:
    return AdapterContext(
        tenant_id=command.tenant_id,
        command_id=str(command.command_id),
        correlation_id=command.correlation_id,
        attempt=1,
        timeout_seconds=5,
        environment="test",
        deployment_sha="test",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(("adapter_id", "command_type", "capability", "base_error", "unknown_error"), CASES)
async def test_post_send_failure_is_unknown_not_transient(
    monkeypatch: pytest.MonkeyPatch,
    adapter_id: str,
    command_type: str,
    capability: str,
    base_error: type,
    unknown_error: type,
) -> None:
    adapter = _registered(monkeypatch, adapter_id)
    command = _command(command_type, adapter_id, capability)

    adapter.legacy = _Raising(unknown_error("outcome unknown after send"))
    after_send = await adapter.execute(command, _context(command))
    assert after_send.outcome is Outcome.UNKNOWN
    assert after_send.error_class is ErrorClass.AMBIGUOUS

    adapter.legacy = _Raising(base_error("refused before send"))
    before_send = await adapter.execute(command, _context(command))
    assert before_send.outcome is Outcome.TRANSIENT
    assert before_send.error_class is ErrorClass.RETRYABLE_BEFORE_EFFECT
