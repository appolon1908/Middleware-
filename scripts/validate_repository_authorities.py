#!/usr/bin/env python3
"""Fail closed when Middleware claims source authority owned by another repository."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "config" / "repository-authorities.v1.json"

EXPECTED = {
    "middleware": "appolon1908-hue/Middleware-",
    "caddy": "appolon1908-hue/Caddy",
    "kong": "appolon1908-hue/Kong",
    "keycloak": "appolon1908-hue/Keycloak",
    "n8n": "appolon1908-hue/N8N",
    "odoo": "appolon1908-hue/Odoo",
    "telnexa-sms": "appolon1908-hue/telnexa",
    "telnexa-web": "appolon1908-hue/Telnexa-web",
    "klyrow-email": "appolon1908-hue/klyrow.com",
    "klyrow-web": "appolon1908-hue/klyrow-Website-",
    "kyqra-crawler": "appolon1908-hue/kyqra-crawler",
    "vicidial-asterisk": "appolon1908-hue/Vicidialer-Codestra",
    "provisioning": "appolon1908-hue/codestra-provisioning-service",
    "sdk": "appolon1908-hue/SDK-repository",
    "social": "appolon1908-hue/social.codestra.co",
}
REFERENCE = "appolon1908-hue/codestra-production-platform"
CONNECTOR_OWNERS = {
    "beyvra-nonfinancial": "beyvra-backend",
    "klyrow-email": "klyrow-email",
    "kyqra-crawler": "kyqra-crawler",
    "odoo-19": "odoo",
    "postly-social": "social",
    "provisioning-service": "provisioning",
    "telnexa-sms": "telnexa-sms",
    "vicidial-restricted": "vicidial-asterisk",
}


def fail(message: str) -> None:
    raise SystemExit(f"REPOSITORY_AUTHORITY_ERROR={message}")


def main() -> None:
    raw = json.loads(REGISTRY.read_text(encoding="utf-8"))
    if raw.get("schema_version") != "1.0":
        fail("unsupported_schema")
    policy = raw.get("policy")
    if not isinstance(policy, dict):
        fail("missing_policy")
    if policy.get("owning_repository_is_principal") is not True:
        fail("owning_repository_rule_disabled")
    if policy.get("central_release_authority") is not False:
        fail("central_release_authority_reintroduced")
    if policy.get("reference_repository") != REFERENCE:
        fail("wrong_reference_repository")

    entries = raw.get("authorities")
    if not isinstance(entries, list) or not entries:
        fail("missing_authorities")
    by_component: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            fail("invalid_authority_entry")
        component = entry.get("component")
        repo = entry.get("principal_repository")
        if not isinstance(component, str) or not isinstance(repo, str):
            fail("invalid_authority_identity")
        if component in by_component:
            fail(f"duplicate_component:{component}")
        if repo == REFERENCE:
            fail(f"reference_repo_cannot_be_principal:{component}")
        if not repo.startswith("appolon1908-hue/"):
            fail(f"non_codestra_principal:{component}")
        by_component[component] = repo

    for component, expected_repo in EXPECTED.items():
        if by_component.get(component) != expected_repo:
            fail(f"wrong_principal:{component}:{by_component.get(component)}")

    refs = raw.get("reference_only")
    if not isinstance(refs, list) or len(refs) != 1:
        fail("reference_only_contract_missing")
    if refs[0].get("repository") != REFERENCE:
        fail("reference_only_repo_changed")

    # Connector manifests are Middleware integration contracts, but their
    # repository field must continue to point at the destination's principal
    # source repository rather than Middleware or the historical platform repo.
    manifest_dir = ROOT / "connectors" / "manifests"
    seen_connectors: set[str] = set()
    for path in sorted(manifest_dir.glob("*.connector.json")):
        manifest = json.loads(path.read_text(encoding="utf-8"))
        connector_id = manifest.get("connector_id")
        repository = manifest.get("repository")
        if connector_id not in CONNECTOR_OWNERS:
            fail(f"unregistered_connector_owner:{connector_id}")
        component = CONNECTOR_OWNERS[connector_id]
        expected_repo = by_component.get(component)
        if repository != expected_repo:
            fail(
                f"connector_repository_drift:{connector_id}:"
                f"{repository}:expected:{expected_repo}"
            )
        if repository in {REFERENCE, "appolon1908-hue/Middleware-"}:
            fail(f"connector_points_to_nonprincipal:{connector_id}")
        seen_connectors.add(connector_id)
    if seen_connectors != set(CONNECTOR_OWNERS):
        fail("connector_manifest_inventory_changed")

    text_targets = [
        ROOT / "README.md",
        ROOT / "docs" / "CI-ENVIRONMENTS-AND-HANDOFF.md",
        ROOT / "docs" / "REPOSITORY-AUTHORITY-POLICY.md",
    ]
    forbidden = (
        "future shared API-edge Caddy source authority** is `appolon1908-hue/Kong`",
        "Caddy's canonical Git home is\n`appolon1908-hue/codestra-production-platform",
        "central deployment manifest authority",
    )
    for path in text_targets:
        if not path.exists():
            fail(f"missing_policy_document:{path.relative_to(ROOT)}")
        text = path.read_text(encoding="utf-8")
        for needle in forbidden:
            if needle in text:
                fail(f"stale_authority_claim:{path.relative_to(ROOT)}")

    print("REPOSITORY_AUTHORITY_POLICY=PASS")
    print("OWNING_REPOSITORY_IS_PRINCIPAL=YES")
    print("CONNECTOR_PRINCIPAL_REPOSITORIES=PASS")
    print("CODESTRA_PRODUCTION_PLATFORM=REFERENCE_ONLY")
    print("CADDY_PRINCIPAL=appolon1908-hue/Caddy")


if __name__ == "__main__":
    main()
