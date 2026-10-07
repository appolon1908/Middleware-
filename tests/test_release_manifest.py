from __future__ import annotations

import json
import os
import subprocess
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from scripts.release_manifest import (
    ROOT,
    ReleaseManifestError,
    build_manifest,
    canonical_json,
    load_manifest,
    validate_manifest,
    verify_workspace,
)


SOURCE_SHA = "a" * 40
TREE_ID = "b" * 40
IMAGE_DIGEST = "sha256:" + ("c" * 64)


def evidence(tmp_path: Path) -> tuple[Path, Path]:
    sbom = tmp_path / "middleware.spdx.json"
    report = tmp_path / "middleware.grype.json"
    sbom.write_text('{"spdxVersion":"SPDX-2.3"}\n', encoding="utf-8")
    report.write_text('{"matches":[]}\n', encoding="utf-8")
    return sbom, report


def manifest(tmp_path: Path) -> dict:
    sbom, report = evidence(tmp_path)
    return build_manifest(
        root=ROOT,
        source_sha=SOURCE_SHA,
        git_tree_id=TREE_ID,
        image_digest=IMAGE_DIGEST,
        built_at="2026-08-28T12:00:00Z",
        run_id=12345,
        run_attempt=1,
        sbom_path=sbom,
        vulnerability_report_path=report,
    )


def test_generated_release_manifest_matches_json_schema(tmp_path: Path) -> None:
    value = manifest(tmp_path)
    schema = json.loads(
        (ROOT / "contracts/release-manifest.v1.schema.json").read_text(encoding="utf-8")
    )
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(value)
    assert value["image"]["reference"].endswith(IMAGE_DIGEST)
    assert value["promotion"]["staging_and_production_same_digest"] is True


def test_manifest_is_canonical_and_binds_workspace_evidence(tmp_path: Path) -> None:
    source_sha = os.environ.get("CODESTRA_TEST_SOURCE_SHA") or subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    tree_id = os.environ.get("CODESTRA_TEST_GIT_TREE_ID") or subprocess.run(
        ["git", "rev-parse", "HEAD^{tree}"], cwd=ROOT, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    sbom, report = evidence(tmp_path)
    value = build_manifest(
        root=ROOT,
        source_sha=source_sha,
        git_tree_id=tree_id,
        image_digest=IMAGE_DIGEST,
        built_at="2026-08-28T12:00:00Z",
        run_id=12345,
        run_attempt=1,
        sbom_path=sbom,
        vulnerability_report_path=report,
    )
    path = tmp_path / "release-manifest.json"
    path.write_bytes(canonical_json(value))

    loaded = load_manifest(path)
    if os.environ.get("CODESTRA_TEST_SOURCE_SHA"):
        with patch(
            "scripts.release_manifest._git_identity",
            return_value=(source_sha, tree_id),
        ):
            verify_workspace(loaded, root=ROOT, evidence_dir=tmp_path)
    else:
        verify_workspace(loaded, root=ROOT, evidence_dir=tmp_path)

    sbom_path = tmp_path / loaded["artifacts"]["sbom"]["path"]
    sbom_path.write_text('{"tampered":true}\n', encoding="utf-8")
    with pytest.raises(ReleaseManifestError, match="evidence digest mismatch"):
        if os.environ.get("CODESTRA_TEST_SOURCE_SHA"):
            with patch(
                "scripts.release_manifest._git_identity",
                return_value=(source_sha, tree_id),
            ):
                verify_workspace(loaded, root=ROOT, evidence_dir=tmp_path)
        else:
            verify_workspace(loaded, root=ROOT, evidence_dir=tmp_path)


def test_manifest_rejects_substitution_and_unknown_fields(tmp_path: Path) -> None:
    value = manifest(tmp_path)
    replaced = deepcopy(value)
    replaced["image"]["digest"] = "sha256:" + ("d" * 64)
    with pytest.raises(ReleaseManifestError, match="image.reference"):
        validate_manifest(replaced)

    extended = deepcopy(value)
    extended["signature"] = "untrusted-inline-value"
    with pytest.raises(ReleaseManifestError, match="fields"):
        validate_manifest(extended)

    with pytest.raises(ReleaseManifestError, match="expected release"):
        validate_manifest(value, expected_source_sha="f" * 40)


def test_manifest_rejects_noncanonical_serialization(tmp_path: Path) -> None:
    value = manifest(tmp_path)
    path = tmp_path / "release-manifest.json"
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")
    with pytest.raises(ReleaseManifestError, match="not canonical"):
        load_manifest(path)
