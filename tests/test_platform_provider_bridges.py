"""Provider bridges over the legacy transports: retry safety, provenance and read-back.

The legacy adapters raise one error class both before transmission
(connection refused) and after it (a timeout or 5xx whose read-back failed,
a 4xx, an answer that did not bind the command identity). Only the first is
a known-safe retry; everything else must reach reconciliation, or a retry
duplicates the e-mail / SMS / CRM write.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast
from uuid import uuid4

import httpx
import pytest

from app.commands import CommandEnvelope, CommandOperation
from app.platform.adapter import AdapterContext, ErrorClass, Outcome, ReadbackStatus
from app.platform.adapters.providers import LegacyBridge, provider_adapters, vicidial_readback_status


class LegacyError(RuntimeError):
    pass


class LegacyUnknownError(LegacyError):
    pass


@dataclass
class ActivityResult:
    status: str
    detail: str = ""
    provider_operation_id: str | None = None
    readback_evidence: dict[str, Any] | None = None


class ScriptedLegacy:
    env: dict[str, str] = {}

    def __init__(self, behaviour: str, readback: ActivityResult | None = None) -> None:
        self.behaviour = behaviour
        self.readback_result = readback or ActivityResult("matched", provider_operation_id="msg-1")
        self.requests: list[Any] = []

    async def execute(self, request: Any) -> ActivityResult:
        self.requests.append(request)
        if self.behaviour == "wrapped_connect":
            # Telnexa/Postly: "connection failed before the submission was sent".
            try:
                raise httpx.ConnectError("refused")
            except httpx.ConnectError as exc:
                raise LegacyError("connection failed before send") from exc
        if self.behaviour == "wrapped_after_send":
            # Klyrow/Odoo/Telnexa: read timeout, then the read-back failed.
            try:
                raise httpx.ReadTimeout("no answer")
            except httpx.ReadTimeout as exc:
                raise LegacyError("outcome remains unknown") from exc
        if self.behaviour == "wrapped_after_readback_failure":
            # Klyrow: the POST failed, the read-back failed, the error chains the read-back.
            try:
                raise httpx.ReadTimeout("no answer")
            except httpx.ReadTimeout:
                try:
                    raise httpx.ConnectError("read-back refused")
                except httpx.ConnectError as readback_error:
                    raise LegacyError("unknown after read-back failed") from readback_error
        if self.behaviour == "rejected_4xx":
            # A 4xx answer: the request was transmitted, no cause chained.
            raise LegacyError("provider rejected with status 409")
        if self.behaviour == "unbound_answer":
            raise LegacyError("response did not bind the command identity")
        if self.behaviour == "bare_connect":
            # The legacy token endpoint refused before the send.
            raise httpx.ConnectError("token endpoint refused")
        if self.behaviour == "unknown_wrapping_connect":
            # A legacy "unknown outcome" subclass whose direct cause is a
            # connection failure (a read-back refused on a fresh connection).
            try:
                raise httpx.ConnectError("read-back refused")
            except httpx.ConnectError as exc:
                raise LegacyUnknownError("outcome remains unknown") from exc
        if self.behaviour == "connect_during_reconcile":
            # Klyrow: the POST was interrupted, the read-back's token fetch is refused.
            try:
                raise httpx.ReadTimeout("no answer")
            except httpx.ReadTimeout:
                raise httpx.ConnectError("token endpoint refused") from None
        return ActivityResult("accepted", provider_operation_id="msg-1")

    async def readback(self, request: Any) -> ActivityResult:
        self.requests.append(request)
        return self.readback_result


def _bridge(legacy: ScriptedLegacy, **overrides: Any) -> LegacyBridge:
    values: dict[str, Any] = {
        "adapter_id": "klyrow-email",
        "provider_family": "email",
        "connector_ids": ("klyrow-email",),
        "served_capabilities": ("EMAIL_DELIVERY",),
        "legacy": legacy,
        "transient_errors": (LegacyError,),
    }
    values.update(overrides)
    return LegacyBridge(**values)


def _envelope(command_type: str = "email.message.send.v1", target: str = "klyrow-email", capability: str = "EMAIL_DELIVERY") -> CommandEnvelope:
    return CommandEnvelope.model_validate(
        {
            "command_id": str(uuid4()),
            "command_type": command_type,
            "command_version": "1.0",
            "target": target,
            "tenant_id": "TEST_SYN",
            "requested_by": "user-1",
            "correlation_id": "corr-" + uuid4().hex[:8],
            "idempotency_key": "idem-" + uuid4().hex,
            "capability": capability,
            "payload": {"to": "x@example.invalid"},
        }
    )


def _context(command: CommandEnvelope, *, client_id: str | None = None) -> AdapterContext:
    return AdapterContext(
        tenant_id=command.tenant_id,
        command_id=str(command.command_id),
        correlation_id=command.correlation_id,
        attempt=1,
        timeout_seconds=5.0,
        environment="test",
        deployment_sha="0" * 40,
        payload=command.payload,
        authenticated_client_id=client_id,
    )


def _operation(command: CommandEnvelope) -> CommandOperation:
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    return CommandOperation(**command.model_dump(exclude={"payload"}), state="accepted", created_at=now, updated_at=now, provider_operation_id="call-1")


@pytest.mark.asyncio
@pytest.mark.parametrize("behaviour", ["wrapped_connect", "bare_connect"])
async def test_only_a_connection_failure_before_send_is_a_known_safe_retry(behaviour: str) -> None:
    command = _envelope()
    result = await _bridge(ScriptedLegacy(behaviour)).execute(command, _context(command))
    assert result.outcome is Outcome.TRANSIENT and result.error_class is ErrorClass.RETRYABLE_BEFORE_EFFECT


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "behaviour",
    ["wrapped_after_send", "wrapped_after_readback_failure", "rejected_4xx", "unbound_answer", "connect_during_reconcile"],
)
async def test_legacy_errors_raised_after_transmission_are_quarantined_not_retried(behaviour: str) -> None:
    command = _envelope()
    result = await _bridge(ScriptedLegacy(behaviour)).execute(command, _context(command))
    assert result.outcome is Outcome.UNKNOWN, behaviour
    assert result.error_class is ErrorClass.AMBIGUOUS


@pytest.mark.asyncio
async def test_bridge_passes_the_authenticated_client_to_the_legacy_provenance_check() -> None:
    legacy = ScriptedLegacy("accepted")
    bridge = _bridge(legacy)
    command = _envelope()
    await bridge.execute(command, _context(command, client_id="odoo-integration"))
    await bridge.readback(_operation(command), _context(command, client_id="odoo-integration"))
    assert [request.authenticated_client_id for request in legacy.requests] == ["odoo-integration", "odoo-integration"]
    # Without a kernel-supplied identity the bridge keeps the worker identity.
    await bridge.execute(command, _context(command))
    assert legacy.requests[-1].authenticated_client_id == "middleware-worker"


@pytest.mark.asyncio
async def test_a_call_still_in_progress_is_not_a_failed_command() -> None:
    nonterminal = ActivityResult("mismatch", "call remains nonterminal or unknown", "call-1", {"terminal": False, "call_state": "ringing"})
    legacy = ScriptedLegacy("accepted", readback=nonterminal)
    bridge = _bridge(
        legacy,
        adapter_id="vicidial-restricted",
        provider_family="telephony",
        connector_ids=("vicidial-restricted",),
        served_capabilities=("INTERNAL_TELEPHONY_CALLS", "PRODUCTION_DIALING"),
        readback_status_of=vicidial_readback_status,
    )
    command = _envelope("telephony-internal.calls.originate", "vicidial-restricted", "INTERNAL_TELEPHONY_CALLS")
    readback = await bridge.readback(_operation(command), _context(command))
    assert readback.status is ReadbackStatus.UNAVAILABLE
    status = await bridge.status(_operation(command), _context(command))
    assert status.outcome is Outcome.UNKNOWN
    legacy.readback_result = ActivityResult("matched", "terminal call evidence verified", "call-1", {"terminal": True})
    assert (await bridge.readback(_operation(command), _context(command))).status is ReadbackStatus.MATCHED


def test_klyrow_is_not_registered_without_its_token_client_secret(test_settings, monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in {
        "KLYROW_EMAIL_API_BASE_URL": "https://klyrow.invalid",
        "KLYROW_EMAIL_MTLS_CA_FILE": "ca.pem",
        "KLYROW_EMAIL_MTLS_CERT_FILE": "cert.pem",
        "KLYROW_EMAIL_MTLS_KEY_FILE": "key.pem",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("KLYROW_EMAIL_OIDC_CLIENT_SECRET_FILE", raising=False)
    assert "klyrow-email" not in {adapter.adapter_id for adapter in provider_adapters(test_settings, http=None)}
    monkeypatch.setenv("KLYROW_EMAIL_OIDC_CLIENT_SECRET_FILE", "secret.txt")
    assert "klyrow-email" in {adapter.adapter_id for adapter in provider_adapters(test_settings, http=None)}


def test_vicidial_is_not_registered_without_its_expected_host(test_settings, monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in {
        "VICIDIAL_INTERNAL_CALL_BASE_URL": "https://serverb.invalid",
        "VICIDIAL_INTERNAL_CALL_SERVICE_IDENTITY": "middleware",
        "VICIDIAL_INTERNAL_CALL_HMAC_FILE": "hmac.key",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("VICIDIAL_INTERNAL_CALL_EXPECTED_HOST", raising=False)
    assert "vicidial-restricted" not in {adapter.adapter_id for adapter in provider_adapters(test_settings, http=None)}


@pytest.mark.asyncio
async def test_a_legacy_unknown_outcome_error_is_never_retried_even_when_it_wraps_a_connect_failure() -> None:
    command = _envelope()
    unguarded = await _bridge(ScriptedLegacy("unknown_wrapping_connect")).execute(command, _context(command))
    assert unguarded.outcome is Outcome.TRANSIENT  # why each provider declares its unknown-outcome class
    guarded = await _bridge(ScriptedLegacy("unknown_wrapping_connect"), unknown_errors=(LegacyUnknownError,)).execute(command, _context(command))
    assert guarded.outcome is Outcome.UNKNOWN and guarded.error_class is ErrorClass.AMBIGUOUS
    assert guarded.safe_error_code == "LegacyUnknownError"


def test_every_configured_provider_bridge_declares_its_unknown_outcome_error(test_settings, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.klyrow_email_adapter import KlyrowEmailAdapterError, KlyrowEmailUnknownOutcomeError
    from app.odoo_provider_adapter import OdooProviderAdapterError, OdooProviderUnknownOutcomeError
    from app.postly_social_adapter import PostlySocialUnknownOutcomeError
    from app.telnexa_provider_adapter import TelnexaProviderAdapterError, TelnexaUnknownOutcomeError

    for name, value in {
        "KLYROW_EMAIL_API_BASE_URL": "https://klyrow.invalid",
        "KLYROW_EMAIL_MTLS_CA_FILE": "ca.pem",
        "KLYROW_EMAIL_MTLS_CERT_FILE": "cert.pem",
        "KLYROW_EMAIL_MTLS_KEY_FILE": "key.pem",
        "KLYROW_EMAIL_OIDC_CLIENT_SECRET_FILE": "secret.txt",
        "TELNEXA_SMS_BASE_URL": "https://telnexa.invalid",
        "TELNEXA_SMS_API_KEY": "key",
        "POSTLY_SOCIAL_BASE_URL": "https://postly.invalid",
        "POSTLY_SOCIAL_API_KEY": "key",
    }.items():
        monkeypatch.setenv(name, value)
    bridges = {adapter.adapter_id: adapter for adapter in provider_adapters(test_settings, http=None)}
    expected = {
        "klyrow-email": KlyrowEmailUnknownOutcomeError,
        "telnexa-sms": TelnexaUnknownOutcomeError,
        "postly-social": PostlySocialUnknownOutcomeError,
    }
    for adapter_id, unknown in expected.items():
        assert adapter_id in bridges, adapter_id
        bridge = cast(Any, bridges[adapter_id])
        assert bridge.unknown_errors == (unknown,), adapter_id
    # The subclasses are what make the declaration necessary: without it the
    # pre-send parent class would classify them.
    assert issubclass(KlyrowEmailUnknownOutcomeError, KlyrowEmailAdapterError)
    assert issubclass(TelnexaUnknownOutcomeError, TelnexaProviderAdapterError)
    assert issubclass(OdooProviderUnknownOutcomeError, OdooProviderAdapterError)
    if "odoo-19" in bridges:
        odoo_bridge = cast(Any, bridges["odoo-19"])
        if odoo_bridge.legacy is not None:
            assert odoo_bridge.unknown_errors == (OdooProviderUnknownOutcomeError,)
