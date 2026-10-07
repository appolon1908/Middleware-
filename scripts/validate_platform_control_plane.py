#!/usr/bin/env python3
"""Validate repository-owned pieces of the platform control-plane contract."""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "contracts" / "platform-control-plane.v1.json"
HMAC_VECTOR_PATH = ROOT / "contracts" / "odoo-hmac-test-vector.v1.json"
MAIN_PATH = ROOT / "app" / "main.py"
N8N_PATH = ROOT / "app" / "n8n_control_plane.py"
ODOO_PATH = ROOT / "app" / "odoo_provider_adapter.py"
WORKER_PATH = ROOT / "workers" / "run_temporal.py"
WORKFLOW_PATH = ROOT / "app" / "temporal_workflows.py"
CAPABILITIES_PATH = ROOT / "config" / "capabilities.v2.json"
ROUTE_AUTHORITY_PATH = ROOT / "config" / "route-authority.v1.json"


def fail(message: str) -> None:
    raise SystemExit(f"PLATFORM_CONTROL_PLANE=FAIL {message}")


def main() -> int:
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    vector = json.loads(HMAC_VECTOR_PATH.read_text(encoding="utf-8"))
    capabilities = json.loads(CAPABILITIES_PATH.read_text(encoding="utf-8"))["capabilities"]
    route_authority = json.loads(ROUTE_AUTHORITY_PATH.read_text(encoding="utf-8"))
    main_source = MAIN_PATH.read_text(encoding="utf-8")
    n8n_source = N8N_PATH.read_text(encoding="utf-8")
    odoo_source = ODOO_PATH.read_text(encoding="utf-8")
    worker_source = WORKER_PATH.read_text(encoding="utf-8")
    workflow_source = WORKFLOW_PATH.read_text(encoding="utf-8")

    if contract.get("status") != "PREPARED_DISABLED":
        fail("source integration must remain PREPARED_DISABLED")
    if contract.get("decision") != "middleware_adopts_automation_v2":
        fail("automation v2 authority decision drifted")
    repositories = contract.get("repositories", {})
    if repositories.get("write_authority") != "appolon1908-hue/Middleware-":
        fail("Middleware repository is not declared write authority")

    edge = contract.get("n8n_to_middleware", {})
    expected_edge = {
        "canonical_submit_path": "/v2/automation/commands",
        "canonical_read_path": "/v2/automation/commands/{command_id}",
        "client_id": "n8n-crm-automation",
        "audience": "middleware-api",
        "submit_scope": "automation.command.crm",
        "read_scope": "automation.command.read",
        "tenant_authority": "verified_token_and_durable_job",
        "header_body_agreement_required": True,
        "direct_provider_access": False,
    }
    for key, value in expected_edge.items():
        if edge.get(key) != value:
            fail(f"n8n edge field {key} drifted")

    automation_authority = route_authority.get("automation", {})
    if route_authority.get("decision") != "middleware_adopts_automation_v2":
        fail("automation v2 authority decision drifted")
    if automation_authority.get("canonical_command_submit") != "POST /v2/automation/commands":
        fail("canonical automation command submit route drifted")
    if automation_authority.get("canonical_command_read") != "GET /v2/automation/commands/{command_id}":
        fail("canonical automation command read route drifted")

    required_n8n_markers = (
        'router = APIRouter(prefix="/v1/integrations/n8n"',
        '@router.post("/commands", deprecated=True)',
        '@router.get("/operations/{command_id}", deprecated=True)',
        'expected_client_id="n8n-automation"',
        'required_scope="middleware.request.forward"',
        'required_scope="middleware.status.read"',
        'authorize_tenant(claims, command.tenant_id)',
        'request.headers.get("Idempotency-Key") != command.idempotency_key',
        '"Deprecation": "true"',
        '"Sunset": _LEGACY_SUNSET',
        'rel="successor-version"',
    )
    missing = [marker for marker in required_n8n_markers if marker not in n8n_source]
    if missing:
        fail("legacy n8n compatibility route drifted: " + ", ".join(missing))
    if "app.include_router(n8n_control_plane_router)" not in main_source:
        fail("legacy n8n compatibility router is not mounted")

    aliases = automation_authority.get("compatibility_aliases")
    if not isinstance(aliases, list) or len(aliases) != 2:
        fail("exactly two n8n v1 compatibility aliases must be declared")
    expected_aliases = {
        "POST /v1/integrations/n8n/commands",
        "GET /v1/integrations/n8n/operations/{command_id}",
    }
    observed_aliases = {
        item.get("route")
        for item in aliases
        if isinstance(item, dict) and item.get("status") == "deprecated"
    }
    if observed_aliases != expected_aliases:
        fail("n8n v1 compatibility alias declaration drifted")

    boundary = contract.get("middleware_to_odoo", {})
    expected_boundary = {
        "target": "odoo-19",
        "capability": "ODOO_WRITE",
        "bridge_module": "codestra_middleware_bridge",
        "canonical_command_type": "crm.lead.upsert",
        "canonical_command_version": "1.0",
        "canonical_command_path": "/codestra/middleware/v1/commands/crm.lead.upsert",
        "canonical_status_path": "/codestra/middleware/v1/commands/{command_id}/status",
        "readback_required": True,
        "unknown_outcome_policy": "query_command_status_before_any_retry",
        "blind_resubmission_allowed": False,
    }
    for key, value in expected_boundary.items():
        if boundary.get(key) != value:
            fail(f"Odoo boundary field {key} drifted")
    expected_hmac_fields = [
        "X-Codestra-Timestamp",
        "X-Codestra-Event-ID",
        "HTTP_METHOD_UPPERCASE",
        "REQUEST_PATH",
        "X-Tenant-ID",
        "X-Correlation-ID",
        "Idempotency-Key",
        "RAW_REQUEST_BODY",
    ]
    if boundary.get("hmac_canonical_fields_in_order") != expected_hmac_fields:
        fail("Odoo HMAC canonical field order drifted")

    required_odoo_markers = (
        'ODOO_LEAD_COMMAND_SCHEMA = ROOT / "contracts" / "odoo-lead-command.schema.json"',
        'UPSERT_LEAD = "crm.lead.upsert"',
        'SUPPORTED = {UPSERT_LEAD}',
        'COMMAND_PATH = "/codestra/middleware/v1/commands/crm.lead.upsert"',
        'STATUS_PATH = "/codestra/middleware/v1/commands/{command_id}/status"',
        'self.settings.external_effects.get("ODOO_WRITE") is not True',
        'len(value) > 255',
        '_odoo_lead_command_validator().iter_errors(document)',
        'request.tenant_id.encode("utf-8")',
        'request.correlation_id.encode("utf-8")',
        'idempotency_key.encode("utf-8")',
        'return await self._reconcile_unknown_write(request, exc)',
        'reconciliation = await self.readback(request)',
        'data.get("operation") != self.UPSERT_LEAD',
        'ODOO_INBOUND_HMAC_SECRET',
    )
    missing = [marker for marker in required_odoo_markers if marker not in odoo_source]
    if missing:
        fail("Odoo adapter implementation drifted: " + ", ".join(missing))
    for forbidden in (
        'CREATE_LEAD = "crm.lead.create.v1"',
        'UPDATE_LEAD = "crm.lead.update.v1"',
        'return "POST", "/codestra/middleware/v1/crm/leads"',
    ):
        if forbidden in odoo_source:
            fail(f"obsolete Odoo adapter contract remains: {forbidden}")
    if "OdooProviderAdapter(settings)" not in worker_source:
        fail("Temporal worker does not register Odoo adapter")
    if "retry_policy=RetryPolicy(maximum_attempts=1)" not in workflow_source:
        fail("command adapter execution may be retried after an unknown outcome")

    canonical = "\n".join(
        (
            vector["timestamp"],
            vector["event_id"],
            vector["method"],
            vector["path"],
            vector["tenant_id"],
            vector["correlation_id"],
            vector["idempotency_key"],
            vector["body_utf8"],
        )
    ).encode("utf-8")
    digest = hmac.new(
        vector["secret"].encode("utf-8"), canonical, hashlib.sha256
    ).hexdigest()
    if digest != vector.get("expected_hmac_sha256_hex"):
        fail("published Odoo HMAC test vector is invalid")
    if vector.get("secret") != "test-secret-not-production":
        fail("HMAC vector must remain synthetic")
    document = json.loads(vector["body_utf8"])
    if vector.get("event_id") != document.get("command_id"):
        fail("HMAC vector event ID does not match command ID")

    if capabilities.get("ODOO_WRITE") is not False:
        fail("ODOO_WRITE must remain false in the source capability registry")
    for flag in ("EMAIL_DELIVERY", "SMS_DELIVERY", "PRODUCTION_DIALING"):
        if capabilities.get(flag) is not False:
            fail(f"{flag} unexpectedly enabled")

    serialized = (
        CONTRACT_PATH.read_text(encoding="utf-8")
        + HMAC_VECTOR_PATH.read_text(encoding="utf-8")
    ).lower()
    for forbidden in ("client_secret", "password", "access_token", "private_key"):
        if forbidden in serialized:
            fail(f"shared contract contains forbidden secret-bearing field: {forbidden}")

    print("PLATFORM_CONTROL_PLANE=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
