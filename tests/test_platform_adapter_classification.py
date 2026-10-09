"""Section 4.5 — normalized result / error classification of the adapter framework.

The classification decides whether the kernel may retry blindly
(RETRYABLE_BEFORE_EFFECT), must fail (NON_RETRYABLE / PROVIDER_AUTH) or must
quarantine for reconciliation (AMBIGUOUS). A wrong answer on the "retry"
side duplicates a provider effect; a wrong answer on the "fail" side buries
an effect that may exist under a terminal state an operator can REEXECUTE.
These cases pin both edges, plus the bounded readiness of Section 4.3.
"""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest

from app.platform.adapter import (
    AdapterContext,
    AdapterReadiness,
    BaseAdapter,
    ErrorClass,
    Outcome,
    classify_status,
    classify_transport_error,
    error_class_for,
)
from app.platform.adapters.fixtures import FixtureAdapter
from app.platform.adapters.providers import LegacyBridge, OdooAdapter
from app.platform.registry import AdapterRegistry
from app.platform.runtime import command_policies


def _status_error(status_code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://provider.invalid/send")
    return httpx.HTTPStatusError("provider answered", request=request, response=httpx.Response(status_code, request=request))


def _context(timeout: float = 0.2) -> AdapterContext:
    return AdapterContext(
        tenant_id="TEST_SYN",
        command_id="readiness",
        correlation_id="corr-readiness",
        attempt=0,
        timeout_seconds=timeout,
        environment="test",
        deployment_sha="0" * 40,
    )


# ----------------------------------------------------------------------------
# HTTP status -> outcome
# ----------------------------------------------------------------------------
@pytest.mark.parametrize("status_code", [502, 504])
def test_gateway_failures_after_transmission_are_ambiguous_not_retryable(status_code: int) -> None:
    # A gateway answering 502/504 already forwarded the request upstream: the
    # provider may have applied the effect. A blind retry would duplicate it.
    assert classify_status(status_code) is Outcome.UNKNOWN


@pytest.mark.parametrize("status_code", [408, 425, 429, 503])
def test_provider_refusals_before_processing_stay_retryable(status_code: int) -> None:
    assert classify_status(status_code) is Outcome.TRANSIENT


@pytest.mark.parametrize(
    ("status_code", "outcome", "error_class"),
    [
        (200, Outcome.COMPLETED, None),
        (202, Outcome.ACCEPTED, None),
        (400, Outcome.REJECTED, ErrorClass.NON_RETRYABLE),
        (401, Outcome.REJECTED, ErrorClass.PROVIDER_AUTH),
        (403, Outcome.REJECTED, ErrorClass.PROVIDER_AUTH),
        (429, Outcome.TRANSIENT, ErrorClass.PROVIDER_RATE_LIMITED),
        (503, Outcome.TRANSIENT, ErrorClass.RETRYABLE_BEFORE_EFFECT),
        (500, Outcome.UNKNOWN, ErrorClass.AMBIGUOUS),
        (502, Outcome.UNKNOWN, ErrorClass.AMBIGUOUS),
    ],
)
def test_base_adapter_normalizes_http_answers_consistently(status_code: int, outcome: Outcome, error_class: ErrorClass | None) -> None:
    result = BaseAdapter().normalize_result({"status_code": status_code, "id": "ref-1"})
    assert result.outcome is outcome
    assert result.error_class is error_class
    assert error_class_for(outcome, status_code) is error_class


def test_base_adapter_does_not_mistake_a_boolean_for_a_status_code() -> None:
    result = BaseAdapter().normalize_result({"status_code": True})
    assert result.outcome is Outcome.UNKNOWN and result.error_class is ErrorClass.AMBIGUOUS


# ----------------------------------------------------------------------------
# exceptions -> error class
# ----------------------------------------------------------------------------
def test_response_decoding_failures_are_ambiguous() -> None:
    # json.JSONDecodeError and UnicodeDecodeError are ValueError subclasses,
    # but they are raised while reading an answer: the request was sent.
    with pytest.raises(json.JSONDecodeError) as decode:
        json.loads("<html>gateway</html>")
    assert classify_transport_error(decode.value) is ErrorClass.AMBIGUOUS
    with pytest.raises(UnicodeDecodeError) as unicode:
        b"\xff\xfe\xfa".decode("utf-8")
    assert classify_transport_error(unicode.value) is ErrorClass.AMBIGUOUS
    assert classify_transport_error(httpx.DecodingError("bad gzip")) is ErrorClass.AMBIGUOUS


def test_pool_timeout_never_left_the_process() -> None:
    assert classify_transport_error(httpx.PoolTimeout("no connection")) is ErrorClass.RETRYABLE_BEFORE_EFFECT


def test_deterministic_validation_errors_stay_non_retryable() -> None:
    assert classify_transport_error(ValueError("bad payload")) is ErrorClass.NON_RETRYABLE
    assert classify_transport_error(KeyError("missing")) is ErrorClass.NON_RETRYABLE


@pytest.mark.parametrize(
    ("status_code", "error_class"),
    [
        (400, ErrorClass.NON_RETRYABLE),
        (401, ErrorClass.PROVIDER_AUTH),
        (403, ErrorClass.PROVIDER_AUTH),
        (429, ErrorClass.PROVIDER_RATE_LIMITED),
        (503, ErrorClass.RETRYABLE_BEFORE_EFFECT),
        (500, ErrorClass.AMBIGUOUS),
        (504, ErrorClass.AMBIGUOUS),
    ],
)
def test_raise_for_status_errors_are_classified_by_their_status(status_code: int, error_class: ErrorClass) -> None:
    assert classify_transport_error(_status_error(status_code)) is error_class


# ----------------------------------------------------------------------------
# Odoo CRM bridge answers
# ----------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("status_code", "outcome", "error_class"),
    [
        (201, Outcome.ACCEPTED, None),
        (401, Outcome.REJECTED, ErrorClass.PROVIDER_AUTH),
        (422, Outcome.REJECTED, ErrorClass.NON_RETRYABLE),
        (429, Outcome.TRANSIENT, ErrorClass.PROVIDER_RATE_LIMITED),
        (503, Outcome.TRANSIENT, ErrorClass.RETRYABLE_BEFORE_EFFECT),
        (504, Outcome.UNKNOWN, ErrorClass.AMBIGUOUS),
    ],
)
def test_odoo_bridge_answers_carry_a_matching_error_class(status_code: int, outcome: Outcome, error_class: ErrorClass | None) -> None:
    result = OdooAdapter().normalize_result({"status_code": status_code, "body": {"profile_id": 5}})
    assert result.outcome is outcome
    assert result.error_class is error_class


@pytest.mark.parametrize("status_code", ["abc", None, 3.5, True])
def test_odoo_unparseable_status_is_unknown_instead_of_raising(status_code: object) -> None:
    # Raising here would surface as a ValueError -> NON_RETRYABLE -> failed,
    # although the CRM write already happened.
    result = OdooAdapter().normalize_result({"status_code": status_code, "body": {}})
    assert result.outcome is Outcome.UNKNOWN and result.error_class is ErrorClass.AMBIGUOUS


# ----------------------------------------------------------------------------
# Section 4.3 — readiness is bounded
# ----------------------------------------------------------------------------
class _HangingReadiness(FixtureAdapter):
    async def readiness(self, context: AdapterContext) -> AdapterReadiness:
        await asyncio.sleep(30)
        return AdapterReadiness(ready=True)


@pytest.mark.asyncio
async def test_registry_readiness_is_bounded_by_the_context_timeout(test_settings) -> None:
    registry = AdapterRegistry(command_policies(test_settings))
    registry.register(
        _HangingReadiness(adapter_id="test-syn", provider_family="synthetic", connector_ids=("test-syn",), served_capabilities=("TEST_SYN_EXECUTE",))
    )
    started = time.perf_counter()
    report = await registry.readiness(_context(timeout=0.1))
    assert time.perf_counter() - started < 5
    assert report["test-syn"].ready is False and report["test-syn"].detail == "readiness_timeout"


class _Legacy:
    env: dict[str, str] = {}

    async def execute(self, request: object) -> object:  # pragma: no cover - not exercised
        raise AssertionError

    async def readback(self, request: object) -> object:  # pragma: no cover - not exercised
        raise AssertionError


@pytest.mark.asyncio
async def test_bridge_readiness_probe_is_bounded_by_the_context_timeout() -> None:
    async def probe() -> bool:
        await asyncio.sleep(30)
        return True

    bridge = LegacyBridge(
        adapter_id="klyrow-email",
        provider_family="email",
        connector_ids=("klyrow-email",),
        served_capabilities=("EMAIL_DELIVERY",),
        legacy=_Legacy(),
        readiness_probe=probe,
    )
    started = time.perf_counter()
    readiness = await bridge.readiness(_context(timeout=0.1))
    assert time.perf_counter() - started < 5
    assert readiness.ready is False and readiness.detail == "readiness_timeout"
