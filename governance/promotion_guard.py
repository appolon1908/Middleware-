#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import pathlib
from datetime import date
from typing import Any

POLICY_PATH = pathlib.Path("governance/promotion-policy.json")


def load_policy(path: pathlib.Path = POLICY_PATH) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("promotion policy must be an object")
    return value


def exception_reason(
    policy: dict[str, Any],
    head: str,
    base: str,
    *,
    today: date | None = None,
) -> str | None:
    current = today or date.today()
    bootstrap = list(policy.get("bootstrap_exceptions") or [])
    legacy_bootstrap = policy.get("bootstrap_exception")
    if isinstance(legacy_bootstrap, dict):
        bootstrap.append(legacy_bootstrap)
    for item in bootstrap:
        if (
            isinstance(item, dict)
            and head == item.get("head")
            and base == item.get("base")
        ):
            return "bootstrap"

    for item in policy.get("migration_exceptions") or []:
        if not isinstance(item, dict):
            continue
        if head != item.get("head") or base != item.get("base"):
            continue
        expires_on = item.get("expires_on")
        reason = item.get("reason")
        if not isinstance(expires_on, str) or not isinstance(reason, str):
            return None
        try:
            expiry = date.fromisoformat(expires_on)
        except ValueError:
            return None
        if current <= expiry:
            return f"migration:{reason}:expires={expires_on}"
        return None
    return None


def promotion_allowed(policy: dict[str, Any], head: str, base: str) -> bool:
    for rule in policy["accepted_promotions"]:
        if not (
            fnmatch.fnmatch(head, rule["head"])
            and fnmatch.fnmatch(base, rule["base"])
        ):
            continue
        if rule.get("relation") == "matching-section-required":
            return (
                head.startswith("subsection/")
                and base.startswith("section/")
                and head[len("subsection/"):].split("--", 1)[0]
                == base[len("section/"):]
            )
        return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--head")
    parser.add_argument("--base")
    args = parser.parse_args()
    head = args.head or os.environ.get("GITHUB_HEAD_REF", "")
    base = args.base or os.environ.get("GITHUB_BASE_REF", "")
    policy = load_policy()

    reason = exception_reason(policy, head, base)
    if reason is not None:
        print(f"PROMOTION_GUARD=PASS {reason} head={head} base={base}")
        return 0

    if not promotion_allowed(policy, head, base):
        print(f"PROMOTION_GUARD=BLOCK head={head} base={base}")
        return 1

    print(f"PROMOTION_GUARD=PASS head={head} base={base}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
