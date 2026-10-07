from __future__ import annotations

from typing import Any

from workers import run_temporal


def test_temporal_worker_wires_all_reviewed_provider_adapters(monkeypatch: Any) -> None:
    settings = object()
    command_store = object()
    markers: dict[str, object] = {}

    def adapter_factory(name: str):
        def build(provided_settings: object) -> object:
            assert provided_settings is settings
            marker = object()
            markers[name] = marker
            return marker

        return build

    captured: dict[str, object] = {}

    class CapturingActivities:
        def __init__(
            self,
            provided_store: object,
            odoo_adapter: object,
            alert_adapter: object,
            *,
            telnexa_sms_adapter: object,
            klyrow_email_adapter: object,
            postly_social_adapter: object,
        ) -> None:
            captured.update(
                {
                    "store": provided_store,
                    "odoo": odoo_adapter,
                    "alert": alert_adapter,
                    "sms": telnexa_sms_adapter,
                    "email": klyrow_email_adapter,
                    "social": postly_social_adapter,
                }
            )

    monkeypatch.setattr(
        run_temporal, "OdooProviderAdapter", adapter_factory("odoo")
    )
    monkeypatch.setattr(
        run_temporal, "KlyrowAlertAdapter", adapter_factory("alert")
    )
    monkeypatch.setattr(
        run_temporal, "TelnexaSmsAdapter", adapter_factory("sms")
    )
    monkeypatch.setattr(
        run_temporal, "KlyrowEmailAdapter", adapter_factory("email")
    )
    monkeypatch.setattr(
        run_temporal, "PostlySocialAdapter", adapter_factory("social")
    )
    monkeypatch.setattr(
        run_temporal, "CommandLedgerWorkflowActivities", CapturingActivities
    )

    activities = run_temporal.build_command_activities(settings, command_store)

    assert isinstance(activities, CapturingActivities)
    assert captured == {
        "store": command_store,
        "odoo": markers["odoo"],
        "alert": markers["alert"],
        "sms": markers["sms"],
        "email": markers["email"],
        "social": markers["social"],
    }
