#!/usr/bin/env python3
from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_SOURCE = "f6748a58f8d2590520a4f28776770957061cdea1"
EXPECTED_DIGEST = "sha256:695fa3ce3f50ba4d0ae0784976b946a0a683ca731155e4bd3bd9e90a4670b820"
EXPECTED_PROFILE = {
    "profile_id": "codestra-middleware-staging-v1",
    "environment": "staging",
    "database": {
        "scheme": "postgresql",
        "host": "postgresql.middleware-staging.svc.cluster.local",
        "port": 5432,
        "name": "codestra_staging",
        "username": "middleware_staging",
        "sslmode": "verify-full",
    },
    "redis": {
        "scheme": "rediss",
        "host": "redis.middleware-staging.svc.cluster.local",
        "port": 6379,
        "database": 14,
        "username": "middleware-staging",
    },
    "nats": {
        "host": "nats.middleware-staging.svc.cluster.local",
        "port": 4222,
        "stream": "CODESTRA_STAGING_EVENTS",
        "subject_prefix": "codestra.staging.events",
    },
    "temporal": {
        "address": "temporal.middleware-staging.svc.cluster.local:7233",
        "namespace": "codestra-staging",
        "task_queue": "codestra-staging-critical",
    },
    "secret_path_prefix": "/run/secrets/middleware-staging-",
    "production_activation_allowed": False,
}
WEBHOOK_SECRET_NAMES = {
    "WEBHOOK_SECRET_ODOO_INTEGRATION",
    "WEBHOOK_SECRET_N8N_AUTOMATION",
    "WEBHOOK_SECRET_VICIDIAL_ADAPTER",
    "WEBHOOK_SECRET_TELNEXA_GATEWAY",
    "WEBHOOK_SECRET_KLYROW_GATEWAY",
    "WEBHOOK_SECRET_KYQRA_GATEWAY",
    "WEBHOOK_SECRET_POSTLY_ADAPTER",
}


def parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        assert separator == "=" and key and key not in values
        values[key] = value
    return values


def assert_database_url(value: str) -> None:
    parsed = urlparse(value)
    assert parsed.scheme == EXPECTED_PROFILE["database"]["scheme"]
    assert parsed.hostname == EXPECTED_PROFILE["database"]["host"]
    assert parsed.port == EXPECTED_PROFILE["database"]["port"]
    assert unquote(parsed.username or "") == EXPECTED_PROFILE["database"]["username"]
    assert parsed.password == "REPLACE_WITH_DATABASE_SECRET"
    assert unquote(parsed.path.lstrip("/")) == EXPECTED_PROFILE["database"]["name"]
    assert parse_qs(parsed.query, strict_parsing=True) == {"sslmode": ["verify-full"]}
    assert not parsed.fragment


def assert_redis_url(value: str) -> None:
    parsed = urlparse(value)
    assert parsed.scheme == EXPECTED_PROFILE["redis"]["scheme"]
    assert parsed.hostname == EXPECTED_PROFILE["redis"]["host"]
    assert parsed.port == EXPECTED_PROFILE["redis"]["port"]
    assert unquote(parsed.username or "") == EXPECTED_PROFILE["redis"]["username"]
    assert parsed.password == "REPLACE_WITH_REDIS_SECRET"
    assert int(parsed.path.lstrip("/")) == EXPECTED_PROFILE["redis"]["database"]
    assert not parsed.query and not parsed.fragment


def main() -> None:
    contract = json.loads((ROOT / "contracts/staging-intake-observability-runtime.v1.json").read_text())
    assert contract["schema_version"] == "1.1"
    release = contract["immutable_release"]
    assert release["source_sha"] == EXPECTED_SOURCE
    assert release["image_digest"] == EXPECTED_DIGEST
    assert release["image_reference"].endswith("@" + EXPECTED_DIGEST)
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", release["image_digest"])
    assert release["schema_head"] == "0003_immutable_event_ledger"

    profiles = json.loads((ROOT / "config/runtime-profiles.v1.json").read_text())
    assert profiles["schema_version"] == "1.0"
    matches = [item for item in profiles["profiles"] if item["profile_id"] == EXPECTED_PROFILE["profile_id"]]
    assert matches == [EXPECTED_PROFILE]
    embedded = release["embedded_runtime_profile"]
    assert embedded == {
        "profile_id": EXPECTED_PROFILE["profile_id"],
        "database_host": EXPECTED_PROFILE["database"]["host"],
        "database_name": EXPECTED_PROFILE["database"]["name"],
        "database_username": EXPECTED_PROFILE["database"]["username"],
        "database_sslmode": EXPECTED_PROFILE["database"]["sslmode"],
        "redis_host": EXPECTED_PROFILE["redis"]["host"],
        "redis_username": EXPECTED_PROFILE["redis"]["username"],
        "redis_database": EXPECTED_PROFILE["redis"]["database"],
        "redis_tls_required": True,
        "production_activation_allowed": False,
    }

    runtime = contract["runtime"]
    assert runtime["environment"] == "staging"
    assert runtime["profile_id"] == EXPECTED_PROFILE["profile_id"]
    assert runtime["allow_in_memory_storage"] is False
    assert runtime["host_ports_published"] is False
    assert runtime["private_network_only"] is True
    assert runtime["dependencies"] == ["postgresql-tls", "redis-tls", "keycloak-jwks"]
    assert runtime["nats_dispatch_mode"] == "disabled"
    assert runtime["temporal_worker_mode"] == "disabled"
    assert runtime["outbox_dispatch_enabled"] is False
    assert runtime["production_dialing"] == "DISABLED"
    assert runtime["production_activation_configured"] is False

    endpoints = contract["authenticated_read_endpoints"]
    expected_endpoints = {
        ("GET", "/metrics", "monitoring-readonly", "metrics.read", "middleware-api", False),
        ("GET", "/v1/runtime/safety", "monitoring-readonly", "health.read", "middleware-api", False),
    }
    actual = {(e["method"], e["path"], e["client_id"], e["scope"], e["audience"], e["public_exposure"]) for e in endpoints}
    assert actual == expected_endpoints and len(endpoints) == 2
    assert contract["token_policy"]["maximum_lifetime_seconds"] == 300
    assert contract["token_policy"]["minimum_independent_tokens"] == 2
    assert contract["token_policy"]["token_values_in_logs_or_artifacts"] is False
    assert contract["runtime_recognized_external_effects"] and all(value is False for value in contract["runtime_recognized_external_effects"].values())
    assert contract["dispatch_controls"] == {
        "OUTBOX_DISPATCH_ENABLED": False,
        "NATS_DISPATCH_MODE": "disabled",
        "TEMPORAL_WORKER_MODE": "disabled",
        "PRODUCTION_DIALING": "DISABLED",
    }
    assert all(value is False for value in contract["defense_in_depth_compatibility_flags"].values())
    assert contract["evidence"]["checksum_state"] == "PENDING_RUNTIME_EXECUTION"
    assert contract["evidence"]["prometheus_target_state"] == "pending"
    assert contract["evidence"]["blackbox_target_state"] == "pending"
    assert contract["production_authorized"] is False

    env = parse_env(ROOT / "config/environments/staging.intake-observability.runtime.env.example")
    assert env["APP_ENV"] == "staging"
    assert env["RUNTIME_PROFILE_ID"] == EXPECTED_PROFILE["profile_id"]
    assert env["APP_SOURCE_SHA"] == EXPECTED_SOURCE
    assert env["IMAGE_DIGEST"] == EXPECTED_DIGEST
    assert env["SCHEMA_HEAD"] == release["schema_head"]
    assert env["ALLOW_IN_MEMORY_STORAGE"] == "false"
    assert_database_url(env["DATABASE_URL"])
    assert_redis_url(env["REDIS_URL"])
    assert env["NATS_URL"] == ""
    assert env["NATS_STREAM"] == EXPECTED_PROFILE["nats"]["stream"]
    assert env["NATS_SUBJECT_PREFIX"] == EXPECTED_PROFILE["nats"]["subject_prefix"]
    assert env["NATS_DISPATCH_MODE"] == "disabled"
    assert env["TEMPORAL_ADDRESS"] == ""
    assert env["TEMPORAL_NAMESPACE"] == EXPECTED_PROFILE["temporal"]["namespace"]
    assert env["TEMPORAL_TASK_QUEUE"] == EXPECTED_PROFILE["temporal"]["task_queue"]
    assert env["TEMPORAL_WORKER_MODE"] == "disabled"
    assert env["PRODUCTION_DIALING"] == "DISABLED"
    for name in contract["runtime_recognized_external_effects"]:
        assert env[name] == "false", name
    assert env["OUTBOX_DISPATCH_ENABLED"] == "false"
    for name in contract["defense_in_depth_compatibility_flags"]:
        assert env[name] == "false", name
    assert WEBHOOK_SECRET_NAMES.issubset(env)
    assert all(len(env[name]) >= 32 and env[name].startswith("REPLACE_WITH_") for name in WEBHOOK_SECRET_NAMES)

    main_source = (ROOT / "app/main.py").read_text()
    security_source = (ROOT / "app/security.py").read_text()
    assert '@app.get("/metrics")' in main_source
    assert 'required_scope="metrics.read"' in main_source
    assert (
        '@app.get("/v1/runtime/safety", response_model=RuntimeSafetyReadback)'
        in main_source
    )
    assert 'required_scope="health.read"' in main_source
    assert "expires_at - issued_at > 300" in security_source
    print("MIDDLEWARE_STAGING_INTAKE_OBSERVABILITY_CONTRACT=PASS")


if __name__ == "__main__":
    main()
