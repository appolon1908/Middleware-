#!/usr/bin/env python3
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def fail(message: str) -> None:
    raise SystemExit(f"OBSERVABILITY_ALERT_CONTRACT=FAIL {message}")


def main() -> None:
    policy = json.loads(
        (ROOT / "config/observability-alert-policy.v1.json").read_text(
            encoding="utf-8"
        )
    )
    capabilities = json.loads(
        (ROOT / "config/capabilities.v2.json").read_text(encoding="utf-8")
    )["capabilities"]
    callers = json.loads(
        (ROOT / "config/control-plane-callers.v1.json").read_text(
            encoding="utf-8"
        )
    )["callers"]
    commands = json.loads(
        (ROOT / "connectors/generated/command-registry.v1.json").read_text(
            encoding="utf-8"
        )
    )["commands"]
    adapters = json.loads(
        (ROOT / "config/adapter-registry.v2.json").read_text(encoding="utf-8")
    )["adapters"]
    contract_source = (
        ROOT / "contracts/observability/alert-api.v1.openapi.yaml"
    ).read_text(encoding="utf-8")
    contract_paths = set(
        re.findall(r"^  (/[^:]+):\s*$", contract_source, flags=re.MULTILINE)
    )
    compose_source = (
        ROOT / "deploy/observability-alerts/compose.core-production.yaml"
    ).read_text(encoding="utf-8")
    service_match = re.search(
        r"^  observability-alert-api:\n(?P<body>.*?)(?=^\S|\Z)",
        compose_source,
        flags=re.MULTILINE | re.DOTALL,
    )
    if service_match is None:
        fail("observability alert API Compose service is missing")
    service_source = service_match.group("body")
    api_source = (
        (ROOT / "app/observability_alerts.py").read_text(encoding="utf-8")
        + (ROOT / "app/observability_alert_contract.py").read_text(
            encoding="utf-8"
        )
        + (ROOT / "app/observability_incidents.py").read_text(encoding="utf-8")
    )
    adapter_source = (ROOT / "app/klyrow_alert_adapter.py").read_text(
        encoding="utf-8"
    )
    worker_source = (ROOT / "workers/run_temporal.py").read_text(
        encoding="utf-8"
    )

    expected = {
        "recipient": "appolon@codestra.co",
        "sender": "alerts@codestra.co",
        "reply_to": "appolon@codestra.co",
        "normal_delivery_path": "middleware-klyrow-adapter",
        "direct_smtp_allowed": False,
        "delivery_enabled_by_default": False,
    }
    for key, value in expected.items():
        if policy.get(key) != value:
            fail(f"policy field drifted: {key}")
    if policy.get("allowed_environments") != ["production"]:
        fail("only production is allowed by the checked-in alert policy")
    if set(policy.get("allowed_severities", [])) != {
        "critical",
        "high",
        "warning",
        "info",
    }:
        fail("alert severity allowlist drifted")
    if policy.get("immediate_severities") != ["critical", "high"]:
        fail("critical/high immediate-delivery policy drifted")
    if policy.get("grouped_severities") != ["warning"]:
        fail("warning grouping policy drifted")
    if policy.get("state_only_severities") != ["info"]:
        fail("informational state-only policy drifted")
    if policy.get("warning_repeat_interval_seconds") != 14400:
        fail("warning repeat interval drifted")
    if capabilities.get("OBSERVABILITY_ALERT_EMAIL_DELIVERY") is not False:
        fail("alert capability must default false")
    if any(value is not False for value in capabilities.values()):
        fail("all repository capabilities must remain false")

    alertmanager = callers.get("alertmanager-service", {})
    if alertmanager.get("command_scope") != "observability.alerts.write":
        fail("Alertmanager write scope drifted")
    if alertmanager.get("status_scope") != "observability.alerts.read":
        fail("Alertmanager read scope drifted")
    if alertmanager.get("allowed_command_prefixes") != ["observability.alert."]:
        fail("Alertmanager command prefix drifted")
    if alertmanager.get("allowed_targets") != ["klyrow-alert-email"]:
        fail("Alertmanager target drifted")

    adapter_caller = callers.get("klyrow-alert-adapter", {})
    if adapter_caller.get("command_scope") != "observability.alerts.events.write":
        fail("Klyrow delivery-event scope drifted")
    if adapter_caller.get("allowed_targets") != ["klyrow-alert-email"]:
        fail("Klyrow delivery-event target drifted")

    operator = callers.get("observability-operator", {})
    if operator.get("command_scope") != "observability.incidents.write":
        fail("observability operator write scope drifted")
    if operator.get("status_scope") != "observability.incidents.read":
        fail("observability operator read scope drifted")
    if operator.get("connector_commands_allowed") is not False:
        fail("observability operator must not receive connector authority")
    if operator.get("allowed_command_prefixes") or operator.get("allowed_targets"):
        fail("observability operator connector authority must remain empty")

    command = next(
        (item for item in commands if item.get("prefix") == "observability.alert."),
        None,
    )
    if command is None:
        fail("alert command policy is missing")
    if command.get("connector_id") != "klyrow-alert-email":
        fail("alert command target drifted")
    if command.get("required_capability") != "OBSERVABILITY_ALERT_EMAIL_DELIVERY":
        fail("alert command capability drifted")
    if command.get("readback_required") is not True:
        fail("provider read-back must remain required")
    if command.get("unknown_outcome_requires_readback") is not True:
        fail("unknown outcome must require read-back")

    adapter = next(
        (item for item in adapters if item.get("id") == "klyrow-alert-email"),
        None,
    )
    if (
        adapter is None
        or adapter.get("repository") != "appolon1908-hue/klyrow.com"
    ):
        fail("Klyrow alert adapter authority is missing or incorrect")
    if adapter.get("direct_n8n") is not False:
        fail("n8n cannot call the alert provider directly")

    required_paths = {
        "/health",
        "/readiness",
        "/version",
        "/capabilities",
        "/v1/integrations/alertmanager/events",
        "/v1/integrations/alertmanager/status-events",
        "/v1/observability/alerts",
        "/v1/observability/alerts/{operation_id}",
        "/v1/observability/alerts/{operation_id}/events",
        "/v1/observability/incidents",
        "/v1/observability/incidents/{incident_id}",
        "/v1/observability/incidents/{incident_id}/timeline",
        "/v1/observability/incidents/{incident_id}/notification-attempts",
        "/v1/observability/incidents/{incident_id}/acknowledge",
        "/v1/observability/incidents/{incident_id}/resolve",
        "/v1/observability/incidents/{incident_id}/reopen",
        "/v1/observability/alert-delivery-events",
        "/metrics",
    }
    if contract_paths != required_paths:
        fail("OpenAPI route inventory drifted")

    image_authority = (
        "image: ${MIDDLEWARE_IMAGE:?set exact "
        "registry/repository@sha256:<digest>}"
    )
    if image_authority not in service_source:
        fail("production image must require an immutable digest")
    if (
        "read_only: true" not in service_source
        or "cap_drop: [ALL]" not in service_source
    ):
        fail("container hardening drifted")
    if re.search(r"^    ports:\s*$", service_source, flags=re.MULTILINE):
        fail("observability alert API must not publish a host port")

    required_markers = (
        'COMMAND_TYPE = "observability.alert.email.send.v1"',
        'COMMAND_TARGET = "klyrow-alert-email"',
        'COMMAND_CAPABILITY = "OBSERVABILITY_ALERT_EMAIL_DELIVERY"',
        "recipient_policy_id",
        "direct_smtp_allowed",
        'authoritative_completion": "provider-readback"',
        "PostgresIncidentStore",
        "request_idempotency_key",
        "X-Source-Deployment",
    )
    if any(marker not in api_source for marker in required_markers):
        fail("alert API implementation marker is missing")
    migration = (ROOT / "migrations/0009_observability_incidents.sql").read_text(
        encoding="utf-8"
    )
    for marker in (
        "middleware_observability_incidents",
        "middleware_observability_incident_events",
        "middleware_observability_incident_audit",
        "middleware_observability_notification_intents",
        "middleware_observability_incident_mutations",
        "request_idempotency_key",
        "notification_repeat",
        "notification_suppressed",
        "REFERENCES middleware_commands(tenant_id,command_id)",
    ):
        if marker not in migration:
            fail(f"incident migration marker missing: {marker}")
    for marker in (
        'MESSAGE_PATH = "/v1/email/messages"',
        'MESSAGE_STATUS_PATH = "/v1/email/messages/{message_id}"',
        'CLIENT_ID = "middleware-alert-delivery"',
        '"appolon@codestra.co"',
        '"alerts@codestra.co"',
        "general LIVE_EMAIL_DELIVERY must remain disabled",
    ):
        if marker not in adapter_source:
            fail(f"Klyrow alert adapter marker missing: {marker}")
    if "KlyrowAlertAdapter(settings)" not in worker_source:
        fail("Temporal worker does not register the Klyrow alert adapter")

    forbidden = ("smtp_password", "smtp_username", "client_secret=")
    serialized = "\n".join(
        path.read_text(encoding="utf-8").lower()
        for path in (
            ROOT / "config/observability-alert-policy.v1.json",
            ROOT / "deploy/observability-alerts/compose.core-production.yaml",
            ROOT / "deploy/observability-alerts/production.env.example",
        )
    )
    if any(item in serialized for item in forbidden):
        fail("secret-bearing alert configuration found")

    print("OBSERVABILITY_ALERT_CONTRACT=PASS")
    print("ALERT_RECIPIENT=appolon@codestra.co")
    print("ALERT_SENDER=alerts@codestra.co")
    print("DIRECT_SMTP_ALLOWED=NO")
    print("ALERT_DELIVERY_DEFAULT=DISABLED")


if __name__ == "__main__":
    main()
