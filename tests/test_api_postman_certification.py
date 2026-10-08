"""The public API surface, its route authority and the Postman collection agree.

Checked against the generators and committed artifacts directly rather than a
pinned digest manifest, so regeneration cannot leave a stale certificate.
"""

from __future__ import annotations

import json
from pathlib import Path

from scripts.generate_postman import build as build_postman

ROOT = Path(__file__).resolve().parents[1]
EDGE = ROOT / "deploy" / "public-api-route-contract.json"
ROUTE_AUTHORITY = ROOT / "config" / "route-authority-report.v1.json"
POSTMAN = ROOT / "postman" / "generated" / "Middleware-OpenAPI.postman_collection.json"


def test_route_authority_reports_no_direct_effect_bypass() -> None:
    report = json.loads(ROUTE_AUTHORITY.read_text(encoding="utf-8"))
    assert report["summary"]["DIRECT_EFFECT_BYPASSES"] == 0


def test_public_edge_never_shares_internal_or_metrics_routes() -> None:
    routes = json.loads(EDGE.read_text(encoding="utf-8"))["routes"]
    assert {row["classification"] for row in routes} <= {"shared_edge", "private_only", "denied"}
    shared = [str(row.get("path", "")) for row in routes if row["classification"] == "shared_edge"]
    assert not [path for path in shared if path.startswith("/internal/") or path == "/metrics"]


def test_committed_postman_collection_is_the_generated_one() -> None:
    collection, _openapi_digest = build_postman()
    assert collection == json.loads(POSTMAN.read_text(encoding="utf-8"))
