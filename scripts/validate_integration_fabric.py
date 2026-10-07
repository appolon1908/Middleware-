#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(path: str) -> dict:
    with (ROOT / path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def validate() -> None:
    ownership = load("config/system-ownership.v2.json")
    capabilities = load("config/capabilities.v2.json")
    registry = load("config/adapter-registry.v2.json")
    command = load("contracts/platform/command-envelope.v1.schema.json")
    command_registry = load("connectors/generated/command-registry.v1.json")
    event = load("contracts/platform/event-envelope.v1.schema.json")
    event_alias = load("contracts/event-envelope.schema.json")
    catalog = load("contracts/platform/contract-catalog.v1.json")

    assert ownership["schema_version"] == "2.0"
    assert ownership["systems"]["middleware"]["owns"]
    assert "direct_provider_write" in ownership["systems"]["n8n"]["forbidden"]
    assert capabilities["default_policy"] == "DENY"
    assert not any(capabilities["capabilities"].values())
    assert command_registry["default_policy"] == "DENY"
    prefixes: set[str] = set()
    for policy in command_registry["commands"]:
        assert policy["prefix"] not in prefixes
        prefixes.add(policy["prefix"])
        assert policy["required_capability"] in capabilities["capabilities"]
        assert capabilities["capabilities"][policy["required_capability"]] is False
        assert policy["readback_required"] is True
        assert policy["unknown_outcome_requires_readback"] is True

    ids: set[str] = set()
    for adapter in registry["adapters"]:
        assert adapter["id"] not in ids
        ids.add(adapter["id"])
        assert adapter["direct_n8n"] is False
        assert adapter["command_prefixes"]
        assert adapter["repository"].startswith("appolon1908-hue/")

    beyvra = next(adapter for adapter in registry["adapters"] if adapter["id"] == "beyvra-nonfinancial")
    assert beyvra["forbidden_prefixes"]
    assert "wallet." in beyvra["forbidden_prefixes"]
    assert command["additionalProperties"] is False
    assert event["additionalProperties"] is False
    assert event_alias["$ref"] == (
        "https://contracts.codestra.co/platform/event-envelope.v1.schema.json"
    )
    assert catalog["canonical"] == {
        "event": "contracts/platform/event-envelope.v1.schema.json",
        "command": "contracts/platform/command-envelope.v1.schema.json",
        "api": "contracts/platform/integration-fabric-api.v2.yaml",
    }
    assert all(
        projection["normalization_required"] is True
        for projection in catalog["wire_projections"]
    )
    assert set(command["required"]) == {
        "command_id",
        "command_type",
        "command_version",
        "target",
        "tenant_id",
        "requested_by",
        "correlation_id",
        "idempotency_key",
        "capability",
        "payload",
    }
    assert set(event["required"]) == {
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
    }


if __name__ == "__main__":
    validate()
    print("CODESTRA_INTEGRATION_FABRIC=PASS")
