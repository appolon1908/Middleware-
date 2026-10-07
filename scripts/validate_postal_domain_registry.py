#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = ROOT / "config" / "postal-domain-registry.json"
SAFETY_PATH = ROOT / "config" / "preproduction-safety.env.example"

VERIFIED = {
    "beyvra.com",
    "breero.com",
    "breero.shop",
    "codestra.agency",
    "codestra.cloud",
    "codestra.co",
    "codestra.digital",
    "codestra.media",
    "klyrow.com",
    "kyqra.com",
    "moneybee.loan",
    "moneybeeloan.com",
    "nativoenglish.com",
    "telnexa.co",
}

UNVERIFIED = {"booked4seasons.com"}
EXPECTED = VERIFIED | UNVERIFIED


def fail(message: str) -> None:
    raise SystemExit(f"POSTAL_DOMAIN_REGISTRY_ERROR={message}")


def main() -> None:
    registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))

    if registry.get("version") != 1:
        fail("unsupported registry version")
    if registry.get("owner") != "middleware":
        fail("middleware must own domain eligibility")
    if registry.get("provider") != "postal":
        fail("provider must be postal")
    if registry.get("workstream") != "integration/postal-email":
        fail("unexpected workstream")

    evidence = registry.get("evidence", {})
    if evidence.get("private_key_material_recorded") is not False:
        fail("private key material must never be recorded")
    if evidence.get("historical_private_key_exposure_reported") is not True:
        fail("historical DKIM exposure must remain acknowledged until rotated")

    policies = registry.get("policies", {})
    required_true = {
        "middleware_is_domain_authority",
        "applications_must_not_submit_directly_to_postal",
        "private_keys_must_not_be_committed",
        "all_configured_domains_require_dkim_rotation",
        "post_rotation_postal_dns_recheck_required",
        "dns_pass_does_not_imply_send_authorization",
    }
    for key in required_true:
        if policies.get(key) is not True:
            fail(f"policy {key} must be true")
    if policies.get("middleware_email_delivery_default") is not False:
        fail("email delivery default must be false")
    if policies.get("external_delivery_default") is not False:
        fail("external delivery default must be false")

    domains = registry.get("domains")
    if not isinstance(domains, list):
        fail("domains must be a list")

    names = [item.get("domain") for item in domains if isinstance(item, dict)]
    if len(names) != len(set(names)):
        fail("duplicate domain")
    if set(names) != EXPECTED:
        fail(f"unexpected domain set: {sorted(set(names) ^ EXPECTED)}")

    by_name = {item["domain"]: item for item in domains}

    for name in VERIFIED:
        item = by_name[name]
        if item.get("postal_configured") is not True:
            fail(f"{name} must remain postal_configured")
        if item.get("latest_postal_dns_check") != "pass":
            fail(f"{name} must record latest Postal DNS pass")
        for check in ("spf", "dkim", "mx", "return_path"):
            if item.get(check) != "ok":
                fail(f"{name} {check} must be ok")
        if item.get("dkim_rotation_required") is not True:
            fail(f"{name} must remain blocked pending DKIM rotation")
        if item.get("post_rotation_recheck_required") is not True:
            fail(f"{name} must require post-rotation recheck")
        if item.get("middleware_send_eligible") is not False:
            fail(f"{name} must not be send eligible")
        if item.get("production_ready") is not False:
            fail(f"{name} must not be production ready")

    booked = by_name["booked4seasons.com"]
    if booked.get("postal_configured") is not True:
        fail("booked4seasons.com must record Postal configuration")
    if booked.get("incoming_configured") is not True or booked.get("outgoing_configured") is not True:
        fail("booked4seasons.com incoming/outgoing configuration must be recorded")
    if booked.get("latest_postal_dns_check") != "never_completed":
        fail("booked4seasons.com must remain unverified")
    for check in ("spf", "dkim", "mx", "return_path"):
        if booked.get(check) != "unknown":
            fail(f"booked4seasons.com {check} must remain unknown")
    if booked.get("dkim_rotation_required") is not True:
        fail("booked4seasons.com must require DKIM rotation")
    if booked.get("middleware_send_eligible") is not False or booked.get("production_ready") is not False:
        fail("booked4seasons.com must fail closed")

    # Reject accidental secret/key material in this registry even if a future edit
    # adds unexpected fields. Public DNS status belongs here; key bytes do not.
    serialized = json.dumps(registry, sort_keys=True)
    forbidden_markers = (
        "BEGIN PRIVATE KEY",
        "BEGIN RSA PRIVATE KEY",
        "BEGIN OPENSSH PRIVATE KEY",
        "PRIVATE_KEY=",
        "DKIM_PRIVATE_KEY",
    )
    for marker in forbidden_markers:
        if marker in serialized:
            fail(f"forbidden secret marker present: {marker}")

    safety = SAFETY_PATH.read_text(encoding="utf-8")
    required_safety = (
        "ENABLE_EXTERNAL_DELIVERY=false",
        "EMAIL_DELIVERY_ENABLED=false",
    )
    for line in required_safety:
        if line not in safety.splitlines():
            fail(f"missing fail-closed safety flag: {line}")

    print(f"POSTAL_DOMAINS_CONFIGURED={len(EXPECTED)}")
    print(f"POSTAL_DNS_VERIFIED={len(VERIFIED)}")
    print(f"POSTAL_DNS_UNVERIFIED={len(UNVERIFIED)}")
    print("DKIM_ROTATION_REQUIRED=ALL_CONFIGURED_DOMAINS")
    print("MIDDLEWARE_EMAIL_DELIVERY_DEFAULT=DISABLED")
    print("POSTAL_DOMAIN_REGISTRY=PASS")


if __name__ == "__main__":
    main()
