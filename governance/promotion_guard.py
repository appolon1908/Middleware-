#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import pathlib
from datetime import date


def exact_pair(rule: dict[str, str], head: str, base: str) -> bool:
    return head == rule.get("head") and base == rule.get("base")


def active_migration_exception(
    policy: dict[str, object], head: str, base: str
) -> str | None:
    rows = policy.get("migration_exceptions", [])
    if not isinstance(rows, list):
        raise SystemExit("PROMOTION_GUARD=BLOCK invalid_migration_exceptions")
    today = date.today()
    for row in rows:
        if not isinstance(row, dict) or not exact_pair(row, head, base):
            continue
        raw_expiry = row.get("expires")
        if not isinstance(raw_expiry, str):
            raise SystemExit("PROMOTION_GUARD=BLOCK invalid_migration_expiry")
        expiry = date.fromisoformat(raw_expiry)
        if today <= expiry:
            return raw_expiry
        print(
            "PROMOTION_GUARD=BLOCK "
            f"expired_migration head={head} base={base} expired={raw_expiry}"
        )
        raise SystemExit(1)
    return None


parser = argparse.ArgumentParser()
parser.add_argument("--head")
parser.add_argument("--base")
args = parser.parse_args()

head = args.head or os.environ.get("GITHUB_HEAD_REF", "")
base = args.base or os.environ.get("GITHUB_BASE_REF", "")
policy = json.loads(pathlib.Path("governance/promotion-policy.json").read_text())

bootstrap_rows: list[dict[str, str]] = []
legacy_bootstrap = policy.get("bootstrap_exception")
if isinstance(legacy_bootstrap, dict):
    bootstrap_rows.append(legacy_bootstrap)
extra_bootstrap = policy.get("bootstrap_exceptions", [])
if isinstance(extra_bootstrap, list):
    bootstrap_rows.extend(row for row in extra_bootstrap if isinstance(row, dict))

if any(exact_pair(row, head, base) for row in bootstrap_rows):
    print(f"PROMOTION_GUARD=PASS bootstrap head={head} base={base}")
    raise SystemExit(0)

expiry = active_migration_exception(policy, head, base)
if expiry is not None:
    print(
        "PROMOTION_GUARD=PASS "
        f"migration head={head} base={base} expires={expiry}"
    )
    raise SystemExit(0)

ok = False
for rule in policy["accepted_promotions"]:
    if fnmatch.fnmatch(head, rule["head"]) and fnmatch.fnmatch(base, rule["base"]):
        ok = True
        if rule.get("relation") == "matching-section-required":
            ok = (
                head.startswith("subsection/")
                and base.startswith("section/")
                and head[len("subsection/") :].split("--", 1)[0]
                == base[len("section/") :]
            )
        if ok:
            break

if not ok:
    print(f"PROMOTION_GUARD=BLOCK head={head} base={base}")
    raise SystemExit(1)

print(f"PROMOTION_GUARD=PASS head={head} base={base}")
