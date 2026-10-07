#!/usr/bin/env python3
"""Validate canonical Codestra identity, API, webhook, and source-readiness contracts."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config"
CANONICAL_ISSUER = "https://auth.codestra.co/realms/codestra"
EXPECTED_CLIENTS = [
    "kong-gateway",
    "middleware-api",
    "middleware-worker",
    "codestra-ai",
    "codestra-communication",
    "codestra-marketing",
    "codestra-social",
    "ai-provider-adapter",
    "marketing-provider-adapter",
    "odoo-integration",
    "n8n-automation",
    "vicidial-adapter",
    "telnexa-gateway",
    "klyrow-gateway",
    "kyqra-gateway",
    "postly-adapter",
    "provisioning-service",
    "monitoring-readonly",
]
EXPECTED_WEBHOOK_PRODUCERS = {
    "odoo-integration",
    "n8n-automation",
    "vicidial-adapter",
    "telnexa-gateway",
    "klyrow-gateway",
    "kyqra-gateway",
    "postly-adapter",
}
EXPECTED_REQUIRED_HEADERS = {
    "Authorization",
    "Content-Type",
    "Idempotency-Key",
    "X-Codestra-Event-Id",
    "X-Codestra-Event-Type",
    "X-Codestra-Source",
    "X-Codestra-Tenant-Id",
    "X-Codestra-Timestamp",
    "X-Codestra-Signature",
    "X-Correlation-Id",
}
EXPECTED_GRANTS: dict[tuple[str, str], set[str]] = {
    ("kong-gateway", "middleware-api"): {
        "middleware.request.forward",
        "middleware.status.read",
    },
    ("middleware-worker", "middleware-api"): {
        "delivery.retry",
        "dlq.replay",
        "inbox.process",
        "outbox.dispatch",
    },
    ("codestra-ai", "middleware-api"): {
        "ai.inference.request",
    },
    ("codestra-communication", "middleware-api"): {
        "communication.email.request",
        "communication.sms.request",
    },
    ("codestra-marketing", "middleware-api"): {
        "marketing.campaign.request",
    },
    ("codestra-social", "middleware-api"): {
        "social.publish.request",
    },
    ("odoo-integration", "middleware-api"): {
        "odoo.delivery.result.publish",
        "odoo.events.publish",
    },
    ("middleware-api", "odoo-integration"): {
        "odoo.activities.write",
        "odoo.leads.read",
        "odoo.leads.write",
    },
    ("middleware-api", "n8n-automation"): {
        "workflow.status.read",
        "workflow.trigger",
    },
    ("n8n-automation", "middleware-api"): {
        "middleware.request.forward",
        "middleware.status.read",
        "workflow.result.publish",
    },
    ("vicidial-adapter", "middleware-api"): {
        "callbacks.update",
        "recordings.metadata.publish",
        "telephony.events.publish",
    },
    ("middleware-api", "vicidial-adapter"): {
        "callbacks.dispatch",
        "telephony.commands.write",
    },
    ("middleware-worker", "telnexa-gateway"): {
        "sms.send",
        "sms.status.read",
    },
    ("telnexa-gateway", "middleware-api"): {
        "sms.events.publish",
        "sms.inbound.publish",
    },
    ("middleware-worker", "klyrow-gateway"): {
        "email.send",
        "email.status.read",
    },
    ("klyrow-gateway", "middleware-api"): {
        "email.events.publish",
        "email.inbound.publish",
    },
    ("middleware-api", "kyqra-gateway"): {
        "crawler.jobs.read",
        "crawler.jobs.submit",
        "crawler.results.read",
    },
    ("kyqra-gateway", "middleware-api"): {
        "crawler.progress.publish",
        "crawler.results.publish",
    },
    ("middleware-worker", "postly-adapter"): {
        "social.publish",
        "social.status.read",
    },
    ("middleware-worker", "ai-provider-adapter"): {
        "ai.provider.dispatch",
        "ai.provider.status.read",
    },
    ("middleware-worker", "marketing-provider-adapter"): {
        "marketing.provider.dispatch",
        "marketing.provider.status.read",
    },
    ("postly-adapter", "middleware-api"): {
        "social.events.publish",
    },
    ("provisioning-service", "middleware-api"): {
        "identity.request",
        "integration.configure",
        "tenant.provision",
    },
    ("monitoring-readonly", "kong-gateway"): {
        "health.read",
        "metrics.read",
    },
    ("monitoring-readonly", "middleware-api"): {
        "health.read",
        "metrics.read",
    },
    ("monitoring-readonly", "odoo-integration"): {
        "health.read",
        "metrics.read",
    },
    ("monitoring-readonly", "n8n-automation"): {
        "health.read",
        "metrics.read",
    },
    ("monitoring-readonly", "vicidial-adapter"): {
        "health.read",
        "metrics.read",
    },
    ("monitoring-readonly", "telnexa-gateway"): {
        "health.read",
        "metrics.read",
    },
    ("monitoring-readonly", "klyrow-gateway"): {
        "health.read",
        "metrics.read",
    },
    ("monitoring-readonly", "kyqra-gateway"): {
        "health.read",
        "metrics.read",
    },
    ("monitoring-readonly", "postly-adapter"): {
        "health.read",
        "metrics.read",
    },
}
ALLOWED_SOURCE_STATES = {
    "contract-only",
    "contract-source-missing",
    "adapter-source-missing",
    "workflow-source-missing",
    "adapter-present-runtime-unverified",
    "gateway-present-runtime-unverified",
    "repository-unconfirmed",
}
RESOURCE_BASE_URL = re.compile(r"^[A-Z][A-Z0-9_]*_BASE_URL$")
SCOPE = re.compile(r"^[a-z][a-z0-9]*(?:[.-][a-z0-9]+)+$")
EVENT_TYPE = re.compile(r"^codestra\.[a-z0-9_]+(?:\.[a-z0-9_]+)+$")
SHA40 = re.compile(r"^[0-9a-f]{40}$")


class ContractError(RuntimeError):
    pass


def fail(message: str) -> None:
    raise ContractError(message)


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"{path}: unable to load JSON: {exc}")
    if not isinstance(value, dict):
        fail(f"{path}: document root must be an object")
    return value


def validate_upstream(value: Any, expected_path: str, label: str) -> None:
    if not isinstance(value, dict):
        fail(f"{label}: upstreamContract must be an object")
    if value.get("repository") != "appolon1908-hue/Keycloak":
        fail(f"{label}: upstream repository must be appolon1908-hue/Keycloak")
    if value.get("path") != expected_path:
        fail(f"{label}: unexpected upstream path")
    if value.get("reviewBranch") != "feat/service-api-webhook-identity-contracts":
        fail(f"{label}: unexpected upstream review branch")
    sha = value.get("reviewSha")
    if not isinstance(sha, str) or not SHA40.fullmatch(sha):
        fail(f"{label}: upstream review SHA must be exact 40-character lowercase hex")


def validate_access(access: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[tuple[str, str], set[str]]]:
    if access.get("schemaVersion") != 1:
        fail("identity-access-map.json: schemaVersion must be 1")
    validate_upstream(
        access.get("upstreamContract"),
        "config/contracts/service-access-matrix.json",
        "identity-access-map.json",
    )
    if access.get("issuer") != CANONICAL_ISSUER:
        fail("identity-access-map.json: issuer is not canonical")
    if access.get("tokenEndpoint") != f"{CANONICAL_ISSUER}/protocol/openid-connect/token":
        fail("identity-access-map.json: token endpoint is not canonical")
    if access.get("jwksUri") != f"{CANONICAL_ISSUER}/protocol/openid-connect/certs":
        fail("identity-access-map.json: JWKS URI is not canonical")

    token_policy = access.get("machineTokenPolicy")
    if not isinstance(token_policy, dict):
        fail("identity-access-map.json: machineTokenPolicy must be an object")
    if token_policy.get("grantType") != "client_credentials":
        fail("machine identities must use client_credentials")
    lifetime = token_policy.get("maximumAccessTokenLifetimeSeconds")
    if not isinstance(lifetime, int) or not 1 <= lifetime <= 300:
        fail("machine access-token lifetime must be 1..300 seconds")
    if token_policy.get("refreshTokensAllowed") is not False:
        fail("machine refresh tokens must be disabled")
    if token_policy.get("fullScopeAllowed") is not False:
        fail("machine full-scope mode must be disabled")
    if token_policy.get("requiredClaims") != [
        "iss",
        "sub",
        "aud",
        "azp",
        "iat",
        "exp",
        "jti",
        "scope",
    ]:
        fail("required machine-token claims changed")

    services = access.get("services")
    if not isinstance(services, list):
        fail("identity-access-map.json: services must be an array")
    ids: list[str] = []
    service_by_id: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(services):
        if not isinstance(raw, dict):
            fail(f"services[{index}] must be an object")
        client_id = raw.get("clientId")
        if client_id not in EXPECTED_CLIENTS:
            fail(f"services[{index}] has an unknown clientId")
        if client_id in service_by_id:
            fail(f"duplicate service: {client_id}")
        if raw.get("audience") != client_id:
            fail(f"{client_id}: audience must equal clientId")
        if not isinstance(raw.get("workstream"), str) or not raw["workstream"]:
            fail(f"{client_id}: workstream is required")
        source_state = raw.get("sourceState")
        if source_state not in ALLOWED_SOURCE_STATES:
            fail(f"{client_id}: invalid or overclaimed sourceState {source_state!r}")
        repository = raw.get("repository")
        if source_state == "repository-unconfirmed":
            if repository is not None:
                fail(f"{client_id}: repository-unconfirmed must not name a repository")
        elif source_state == "contract-only" and repository is None:
            pass
        elif not isinstance(repository, str) or not repository.startswith("appolon1908-hue/"):
            fail(f"{client_id}: an explicit repository is required")
        if not isinstance(raw.get("resourceServer"), bool):
            fail(f"{client_id}: resourceServer must be boolean")
        if raw["resourceServer"]:
            variable = raw.get("baseUrlEnvironment")
            if not isinstance(variable, str) or not RESOURCE_BASE_URL.fullmatch(variable):
                fail(f"{client_id}: resource server requires a *_BASE_URL runtime variable")
        elif "baseUrlEnvironment" in raw:
            fail(f"{client_id}: non-resource workload must not declare a base URL")
        ids.append(client_id)
        service_by_id[client_id] = raw
    if ids != EXPECTED_CLIENTS:
        fail("services must list every canonical machine client in canonical order")

    grants = access.get("grants")
    if not isinstance(grants, list) or not grants:
        fail("identity-access-map.json: grants must be a non-empty array")
    grant_index: dict[tuple[str, str], set[str]] = {}
    for index, raw in enumerate(grants):
        if not isinstance(raw, dict) or set(raw) != {
            "callerClientId",
            "targetClientId",
            "audience",
            "scopes",
        }:
            fail(f"grants[{index}] has an invalid shape")
        caller = raw["callerClientId"]
        target = raw["targetClientId"]
        if caller not in service_by_id or target not in service_by_id or caller == target:
            fail(f"grants[{index}] has an invalid caller or target")
        if raw["audience"] != target:
            fail(f"grants[{index}] audience must equal targetClientId")
        if not service_by_id[target]["resourceServer"]:
            fail(f"grants[{index}] target is not a resource server")
        scopes = raw["scopes"]
        if (
            not isinstance(scopes, list)
            or not scopes
            or scopes != sorted(scopes)
            or len(scopes) != len(set(scopes))
            or not all(isinstance(scope, str) and SCOPE.fullmatch(scope) for scope in scopes)
        ):
            fail(f"grants[{index}] scopes must be sorted, unique, and explicit")
        key = (caller, target)
        if key in grant_index:
            fail(f"duplicate caller-target grant: {caller}->{target}")
        grant_index[key] = set(scopes)

    if grant_index != EXPECTED_GRANTS:
        missing = sorted(set(EXPECTED_GRANTS) - set(grant_index))
        unexpected = sorted(set(grant_index) - set(EXPECTED_GRANTS))
        mismatched = sorted(
            (caller, target, sorted(EXPECTED_GRANTS[(caller, target)]), sorted(grant_index[(caller, target)]))
            for caller, target in set(EXPECTED_GRANTS) & set(grant_index)
            if EXPECTED_GRANTS[(caller, target)] != grant_index[(caller, target)]
        )
        fail(
            "least-privilege grant matrix changed: "
            f"missing={missing}, unexpected={unexpected}, scope_mismatches={mismatched}"
        )

    expected_prohibited = {
        "n8n-automation": [
            "odoo-integration",
            "vicidial-adapter",
            "telnexa-gateway",
            "klyrow-gateway",
            "kyqra-gateway",
            "postly-adapter",
            "ai-provider-adapter",
            "marketing-provider-adapter",
        ]
    }
    if access.get("prohibitedDirectTargets") != expected_prohibited:
        fail("n8n direct-provider prohibition changed")
    for target in expected_prohibited["n8n-automation"]:
        if ("n8n-automation", target) in grant_index:
            fail(f"n8n must not receive a direct grant to {target}")

    for (caller, _target), scopes in grant_index.items():
        if "*" in scopes:
            fail("wildcard scopes are prohibited")
        if caller == "monitoring-readonly" and scopes != {"health.read", "metrics.read"}:
            fail("monitoring-readonly may receive only health.read and metrics.read")

    boundary = access.get("administrativeBoundaries", {}).get("provisioning-service")
    if not isinstance(boundary, dict):
        fail("provisioning-service administrative boundary is missing")
    if boundary.get("keycloakAdminApiAccess") is not False:
        fail("provisioning-service must not receive Keycloak Admin API access")
    if boundary.get("prohibitedRealmManagementRoles") != [
        "realm-admin",
        "manage-realm",
        "manage-clients",
    ]:
        fail("provisioning-service realm-management prohibition changed")

    return service_by_id, grant_index


def validate_webhooks(
    webhooks: dict[str, Any],
    service_by_id: dict[str, dict[str, Any]],
    grant_index: dict[tuple[str, str], set[str]],
) -> None:
    if webhooks.get("schemaVersion") != 1:
        fail("api-webhook-contracts.json: schemaVersion must be 1")
    validate_upstream(
        webhooks.get("upstreamContract"),
        "config/contracts/webhook-contracts.json",
        "api-webhook-contracts.json",
    )
    if webhooks.get("issuer") != CANONICAL_ISSUER:
        fail("api-webhook-contracts.json: issuer is not canonical")
    if webhooks.get("consumerClientId") != "middleware-api":
        fail("middleware-api must be the webhook consumer")
    if webhooks.get("consumerBaseUrlEnvironment") != "MIDDLEWARE_API_BASE_URL":
        fail("webhook consumer URL must use MIDDLEWARE_API_BASE_URL")
    schema_path = webhooks.get("eventEnvelopeSchema")
    if schema_path != "contracts/platform/event-envelope.v1.schema.json":
        fail("webhooks must use the canonical event-envelope schema")
    schema = load_json(ROOT / schema_path)
    required = set(schema.get("required", []))
    for field in {
        "event_id",
        "event_type",
        "event_version",
        "occurred_at",
        "received_at",
        "source",
        "tenant_id",
        "correlation_id",
        "causation_id",
        "idempotency_key",
        "payload",
        "metadata",
    }:
        if field not in required:
            fail(f"event envelope is missing required field {field}")
    pattern = schema.get("properties", {}).get("event_type", {}).get("pattern")
    if not isinstance(pattern, str) or not pattern.startswith("^codestra"):
        fail("event envelope type pattern must require the codestra namespace")

    security = webhooks.get("security")
    if not isinstance(security, dict):
        fail("webhook security policy is missing")
    if security.get("authorization") != "oidc_bearer":
        fail("webhooks must require an OIDC bearer token")
    if security.get("signatureAlgorithm") != "hmac-sha256":
        fail("webhooks must use HMAC-SHA256")
    if security.get("signatureVersion") != "v1":
        fail("webhook signature version must be v1")
    if security.get("maximumClockSkewSeconds") != 300:
        fail("webhook maximum clock skew must be 300 seconds")
    retention = security.get("replayRetentionSeconds")
    if not isinstance(retention, int) or retention < 86400:
        fail("webhook replay retention must be at least 24 hours")
    if set(security.get("requiredHeaders", [])) != EXPECTED_REQUIRED_HEADERS:
        fail("webhook required header set changed")
    if security.get("canonicalSignatureFields") != [
        "version",
        "method",
        "path",
        "timestamp",
        "eventId",
        "sourceClientId",
        "bodySha256",
    ]:
        fail("webhook canonical signature fields changed")
    if security.get("signatureHeaderFormat") != "sha256=<lowercase-hex>":
        fail("webhook signature header format changed")
    if security.get("idempotencyKeySource") != "X-Codestra-Event-Id":
        fail("webhook idempotency source must be X-Codestra-Event-Id")

    hooks = webhooks.get("webhooks")
    if not isinstance(hooks, list) or not hooks:
        fail("api-webhook-contracts.json: webhooks must be a non-empty array")
    producers: set[str] = set()
    hook_ids: set[str] = set()
    paths: set[str] = set()
    event_types: set[str] = set()
    for index, raw in enumerate(hooks):
        if not isinstance(raw, dict) or set(raw) != {
            "id",
            "producerClientId",
            "consumerClientId",
            "audience",
            "requiredScope",
            "path",
            "eventTypes",
            "delivery",
        }:
            fail(f"webhooks[{index}] has an invalid shape")
        hook_id = raw["id"]
        if not isinstance(hook_id, str) or hook_id in hook_ids:
            fail(f"webhooks[{index}] has a duplicate or invalid id")
        hook_ids.add(hook_id)
        producer = raw["producerClientId"]
        consumer = raw["consumerClientId"]
        if producer not in service_by_id or consumer != "middleware-api":
            fail(f"webhooks[{index}] has an invalid producer or consumer")
        if raw["audience"] != "middleware-api":
            fail(f"webhooks[{index}] must target the middleware-api audience")
        scope = raw["requiredScope"]
        if scope not in grant_index.get((producer, consumer), set()):
            fail(f"webhooks[{index}] required scope is not granted")
        path = raw["path"]
        if (
            not isinstance(path, str)
            or not path.startswith("/api/v1/")
            or "://" in path
            or path in paths
        ):
            fail(f"webhooks[{index}] has an invalid or duplicate relative path")
        paths.add(path)
        if raw["delivery"] != "at_least_once":
            fail(f"webhooks[{index}] must use at_least_once delivery")
        values = raw["eventTypes"]
        if (
            not isinstance(values, list)
            or not values
            or values != sorted(values)
            or len(values) != len(set(values))
        ):
            fail(f"webhooks[{index}] event types must be sorted and unique")
        for event_type in values:
            if not isinstance(event_type, str) or not EVENT_TYPE.fullmatch(event_type):
                fail(f"webhooks[{index}] has a non-canonical event type")
            if event_type in event_types:
                fail(f"duplicate event type: {event_type}")
            event_types.add(event_type)
        producers.add(producer)
    if producers != EXPECTED_WEBHOOK_PRODUCERS:
        fail(
            "every adapter/provider must publish a webhook contract: "
            f"expected {sorted(EXPECTED_WEBHOOK_PRODUCERS)}, found {sorted(producers)}"
        )

    connectivity = load_json(CONFIG / "connectivity-map.json")
    policies = connectivity.get("policies", {})
    expected_policy = {
        "all_links_require_explicit_authentication": True,
        "all_effectful_delivery_requires_idempotency": True,
        "webhooks_require_signature_timestamp_and_replay_protection": True,
        "external_effects_disabled_by_default": True,
        "runtime_verification_required_before_activation": True,
        "direct_deployment_from_workstream_branches": False,
    }
    for key, expected in expected_policy.items():
        if policies.get(key) is not expected:
            fail(f"connectivity policy {key} must be {expected!r}")
    dependencies = connectivity.get("workstream_dependencies", {})
    keycloak_bound_workstreams = {
        "platform/kong",
        "integration/odoo-19",
        "integration/n8n",
        "integration/vicidial",
        "integration/telnexa-sms",
        "integration/klyrow-email",
        "integration/kyqra",
        "integration/postly-social",
    }
    for service in service_by_id.values():
        workstream = service["workstream"]
        if workstream not in dependencies:
            fail(f"service workstream is absent from connectivity-map.json: {workstream}")
        if workstream in keycloak_bound_workstreams:
            if "integration/keycloak" not in dependencies[workstream]:
                fail(f"{workstream} must depend on integration/keycloak")


def validate() -> None:
    access = load_json(CONFIG / "identity-access-map.json")
    webhooks = load_json(CONFIG / "api-webhook-contracts.json")
    service_by_id, grant_index = validate_access(access)
    validate_webhooks(webhooks, service_by_id, grant_index)

    source_states: dict[str, int] = {}
    for service in service_by_id.values():
        source_states[service["sourceState"]] = source_states.get(service["sourceState"], 0) + 1

    print(f"SERVICE_CLIENTS={len(service_by_id)}")
    print(f"SERVICE_GRANTS={len(grant_index)}")
    print(f"WEBHOOK_CONTRACTS={len(webhooks['webhooks'])}")
    print(f"WEBHOOK_EVENT_TYPES={sum(len(item['eventTypes']) for item in webhooks['webhooks'])}")
    print(f"SOURCE_STATE_COUNTS={json.dumps(source_states, sort_keys=True, separators=(',', ':'))}")
    print("IDENTITY_ACCESS_POLICY=PASS")
    print("API_WEBHOOK_CONTRACT_POLICY=PASS")
    print("MIDDLEWARE_INTEGRATION_CONTRACTS=PASS")


if __name__ == "__main__":
    try:
        validate()
    except ContractError as exc:
        print(f"IDENTITY_WEBHOOK_CONTRACT_ERROR={exc}", file=sys.stderr)
        raise SystemExit(1)
