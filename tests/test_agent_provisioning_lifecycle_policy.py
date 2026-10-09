"""Database-free rules of the agent provisioning lifecycle."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from app.adapters.vicidial.mtls_client import VicidialMtlsError
from app.api.v1.agent_provisioning import _binding_view
from app.api.v1.agent_provisioning_reads import _channel_state
from app.core.agent_provisioning_lifecycle import (
    IN_FLIGHT_STATES,
    SETTLED_STATES,
    action_allowed,
    classify_vicidial_error,
    is_retryable,
    lease_active,
)

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)


def _status_error(status: int) -> VicidialMtlsError:
    request = httpx.Request("POST", "https://edge.internal.codestra.agency:8443/v1/x")
    cause = httpx.HTTPStatusError(
        "status", request=request, response=httpx.Response(status, request=request),
    )
    try:
        raise VicidialMtlsError("VICidial private request failed closed") from cause
    except VicidialMtlsError as exc:
        return exc


def _chained(cause: Exception) -> VicidialMtlsError:
    try:
        raise VicidialMtlsError("VICidial private request failed closed") from cause
    except VicidialMtlsError as exc:
        return exc


@pytest.mark.parametrize("state", sorted(SETTLED_STATES))
def test_reconcile_is_allowed_from_every_settled_state(state):
    assert action_allowed("reconcile", state, lease_is_active=False)


@pytest.mark.parametrize("state", ["SUSPENDED", "REVOKED"])
def test_reconcile_never_resurrects_withdrawn_access(state):
    assert not action_allowed("reconcile", state, lease_is_active=False)


def test_reactivate_only_from_suspended():
    assert action_allowed("reactivate", "SUSPENDED", lease_is_active=False)
    for state in sorted(SETTLED_STATES | {"REVOKED"} | IN_FLIGHT_STATES):
        assert not action_allowed("reactivate", state, lease_is_active=False)


@pytest.mark.parametrize("state", sorted(SETTLED_STATES | {"SUSPENDED", "REVOKED"}))
def test_revoke_is_always_reachable_outside_a_running_saga(state):
    assert action_allowed("revoke", state, lease_is_active=False)


@pytest.mark.parametrize("action", ["reconcile", "suspend", "revoke", "reactivate"])
@pytest.mark.parametrize("state", sorted(IN_FLIGHT_STATES))
def test_a_running_saga_blocks_every_action(action, state):
    assert not action_allowed(action, state, lease_is_active=True)


@pytest.mark.parametrize("action", ["reconcile", "suspend", "revoke"])
def test_an_abandoned_saga_can_be_recovered_or_withdrawn(action):
    assert action_allowed(action, "CHANNEL_PROVISIONING", lease_is_active=False)


def test_unknown_action_is_denied():
    assert not action_allowed("delete", "EFFECTIVE", lease_is_active=False)


def test_lease_is_only_held_by_fresh_in_flight_rows():
    fresh = NOW - timedelta(seconds=10)
    stale = NOW - timedelta(seconds=301)
    assert lease_active("IDENTITY", fresh, now=NOW, lease_seconds=300)
    assert not lease_active("IDENTITY", stale, now=NOW, lease_seconds=300)
    assert not lease_active("EFFECTIVE", fresh, now=NOW, lease_seconds=300)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (409, "EXTENSION_CONFLICT"),
        (401, "VICIDIAL_UNAUTHORIZED"),
        (403, "VICIDIAL_UNAUTHORIZED"),
        (422, "VICIDIAL_REJECTED"),
        (500, "VICIDIAL_UNAVAILABLE"),
        (503, "VICIDIAL_UNAVAILABLE"),
    ],
)
def test_vicidial_status_errors_map_to_stable_codes(status, expected):
    exc = _status_error(status)
    assert classify_vicidial_error(exc, conflict_code="EXTENSION_CONFLICT") == expected


def test_vicidial_transport_errors_are_unavailable_and_retryable():
    request = httpx.Request("POST", "https://edge.internal.codestra.agency:8443/v1/x")
    for cause in (
        httpx.ConnectTimeout("timeout", request=request),
        httpx.ConnectError("refused", request=request),
    ):
        code = classify_vicidial_error(_chained(cause), conflict_code="X")
        assert code == "VICIDIAL_UNAVAILABLE"
        assert is_retryable(code)


def test_unchained_vicidial_error_is_a_generic_adapter_error():
    code = classify_vicidial_error(VicidialMtlsError("disabled"), conflict_code="X")
    assert code == "VICIDIAL_ADAPTER_ERROR"


@pytest.mark.parametrize(
    "code",
    [
        "VICIDIAL_REJECTED", "VICIDIAL_UNAUTHORIZED", "EXTENSION_CONFLICT",
        "AGENT_BINDING_CONFLICT", "EXTENSION_BINDING_CONFLICT",
        "IDENTITY_BINDING_CONFLICT", "READBACK_MISMATCH", "VICIDIAL_NOT_CONFIGURED",
        None,
    ],
)
def test_conflicts_and_rejections_are_not_retryable(code):
    assert not is_retryable(code)


def _request(state: str, **channels) -> SimpleNamespace:
    return SimpleNamespace(state=state, keycloak_subject="kc-1", channels_json=channels)


def _step(system: str, operation: str, reference: str, state: str = "succeeded"):
    return SimpleNamespace(
        system=system, operation=operation, state=state, external_reference=reference,
        error_code=None, error_summary=None, completed_at=NOW,
    )


PROVISIONED = [
    _step("keycloak", "create_user", "kc-1"),
    _step("vicidial", "sync_agent", "COD0016"),
    _step("vicidial", "reserve_extension", "6203"),
    _step("vicidial", "provision_webrtc", "6203"),
]
DEPROVISIONED = PROVISIONED + [
    _step("vicidial", "revoke_webrtc", "6203"),
    _step("vicidial", "disable_agent", "COD0016"),
    _step("keycloak", "disable_user", "kc-1"),
]


def test_binding_view_reports_only_provider_confirmed_bindings():
    failed_adopt = PROVISIONED[:2] + [_step("vicidial", "adopt_extension", "6101", "failed")]
    assert _binding_view(_request("FAILED"), failed_adopt) == {
        "keycloak_subject": "kc-1", "vicidial_user_id": "COD0016", "agent_state": "synced",
        "extension": None, "extension_mode": None, "webrtc_state": None,
    }


def test_binding_view_tracks_deprovisioning_but_keeps_the_extension_binding():
    assert _binding_view(_request("REVOKED"), DEPROVISIONED) == {
        "keycloak_subject": "kc-1", "vicidial_user_id": "COD0016", "agent_state": "disabled",
        "extension": "6203", "extension_mode": "reserved", "webrtc_state": "revoked",
    }


@pytest.mark.parametrize("channel", ["phone", "webrtc", "odoo"])
def test_channels_grant_access_only_while_provisioned_and_not_withdrawn(channel):
    channels = {"odoo": True, "phone": True, "webrtc": True}
    assert _channel_state(channel, _request("EFFECTIVE", **channels), PROVISIONED)[
        "effective_access"
    ] is True
    # Deprovisioned at the provider.
    assert _channel_state(channel, _request("REVOKED", **channels), DEPROVISIONED)[
        "effective_access"
    ] is False
    # Withdrawn by lifecycle state even while a provider step is still live
    # (a suspend whose deprovision steps were gated or failed).
    assert _channel_state(channel, _request("SUSPENDED", **channels), PROVISIONED)[
        "effective_access"
    ] is False


def test_reactivated_identity_grants_odoo_access_again():
    reactivated = DEPROVISIONED + [_step("keycloak", "enable_user", "kc-1")]
    state = _channel_state("odoo", _request("EFFECTIVE", odoo=True), reactivated)
    assert state["requested_state"] == "enable_user"
    assert state["effective_access"] is True
