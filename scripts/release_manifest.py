#!/usr/bin/env python3
"""Create and verify the canonical signed-release predicate.

The JSON document is deliberately dependency-free and canonicalized before
signing. Signature verification is delegated to Cosign so verification uses the
Sigstore certificate identity, OIDC issuer, and transparency-log bundle.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = "1.0"
SERVICE = "middleware-api"
REPOSITORY = "appolon1908-hue/Middleware-"
SOURCE_REF = "refs/heads/main"
IMAGE_REPOSITORY = "ghcr.io/appolon1908-hue/codestra-middleware"
PLATFORMS = ["linux/amd64"]
BASE_IMAGE = "gcr.io/distroless/python3-debian13:nonroot@sha256:f3d5ddc6c64a019fe520e7f005f2880be21e6afc461b10a3c15ef2e4edc71e33"
WORKFLOW_PATH = ".github/workflows/release.yml"
CERTIFICATE_IDENTITY = (
    "https://github.com/appolon1908-hue/Middleware-/"
    ".github/workflows/release.yml@refs/heads/main"
)
OIDC_ISSUER = "https://token.actions.githubusercontent.com"
SHA40 = re.compile(r"^[0-9a-f]{40}$")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
EVIDENCE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}\.json$")

FIXED_ARTIFACTS: dict[str, tuple[str, str]] = {
    "dependency_input": ("file", "requirements-runtime.in"),
    "dependency_lock": ("file", "requirements-runtime.txt"),
    "test_dependency_input": ("file", "requirements-test.in"),
    "test_dependency_lock": ("file", "requirements-test.txt"),
    "dockerfile": ("file", "Dockerfile.runtime"),
    "runtime_profiles": ("file", "config/runtime-profiles.v1.json"),
    "contracts_bundle": ("tree", "contracts"),
    "migrations_bundle": ("tree", "migrations"),
}


class ReleaseManifestError(RuntimeError):
    """The release evidence is incomplete, non-canonical, or inconsistent."""


def sha256_file(path: Path) -> str:
    if not path.is_file() or path.is_symlink():
        raise ReleaseManifestError(f"release artifact is not a regular file: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def sha256_tree(path: Path) -> str:
    if not path.is_dir() or path.is_symlink():
        raise ReleaseManifestError(f"release artifact is not a directory: {path}")
    files = sorted(item for item in path.rglob("*") if item.is_file())
    if not files:
        raise ReleaseManifestError(f"release artifact directory is empty: {path}")
    digest = hashlib.sha256(b"codestra-release-tree-v1\0")
    for item in files:
        if item.is_symlink():
            raise ReleaseManifestError(f"release artifact contains a symlink: {item}")
        relative = item.relative_to(path).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(bytes.fromhex(sha256_file(item).removeprefix("sha256:")))
    return "sha256:" + digest.hexdigest()


def canonical_json(value: dict[str, Any]) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _schema_head(root: Path) -> str:
    migrations = sorted((root / "migrations").glob("[0-9][0-9][0-9][0-9]_*.sql"))
    if not migrations:
        raise ReleaseManifestError("release has no numbered migrations")
    return migrations[-1].stem


def _runtime_profile_ids(root: Path) -> list[str]:
    try:
        value = json.loads(
            (root / "config/runtime-profiles.v1.json").read_text(encoding="utf-8")
        )
        identifiers = [item["profile_id"] for item in value["profiles"]]
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ReleaseManifestError("runtime profile identities cannot be loaded") from exc
    expected = [
        "codestra-middleware-production-compose-v1",
        "codestra-middleware-production-v1",
        "codestra-middleware-staging-v1",
    ]
    if sorted(identifiers) != expected:
        raise ReleaseManifestError("runtime profile identities are not canonical")
    return expected


def _fixed_artifact_entries(root: Path) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for key, (kind, relative) in FIXED_ARTIFACTS.items():
        path = root / relative
        digest = sha256_tree(path) if kind == "tree" else sha256_file(path)
        result[key] = {"path": relative, "sha256": digest}
    return result


def _git_identity(root: Path) -> tuple[str, str]:
    try:
        source_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        tree_id = subprocess.run(
            ["git", "rev-parse", "HEAD^{tree}"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ReleaseManifestError("Git source identity cannot be verified") from exc
    if SHA40.fullmatch(source_sha) is None or SHA40.fullmatch(tree_id) is None:
        raise ReleaseManifestError("Git returned a non-canonical source identity")
    return source_sha, tree_id


def verify_source_checkout(
    *,
    root: Path,
    expected_source_sha: str,
    expected_git_tree_id: str,
) -> None:
    source_sha, tree_id = _git_identity(root)
    if source_sha != expected_source_sha.lower():
        raise ReleaseManifestError("checked-out Git SHA does not match manifest")
    if tree_id != expected_git_tree_id.lower():
        raise ReleaseManifestError("checked-out Git tree does not match manifest")


def build_manifest(
    *,
    root: Path,
    source_sha: str,
    git_tree_id: str,
    image_digest: str,
    built_at: str,
    run_id: int,
    run_attempt: int,
    sbom_path: Path,
    vulnerability_report_path: Path,
) -> dict[str, Any]:
    source_sha = source_sha.strip().lower()
    git_tree_id = git_tree_id.strip().lower()
    image_digest = image_digest.strip().lower()
    artifacts = _fixed_artifact_entries(root)
    artifacts["sbom"] = {
        "path": sbom_path.name,
        "sha256": sha256_file(sbom_path),
    }
    artifacts["vulnerability_report"] = {
        "path": vulnerability_report_path.name,
        "sha256": sha256_file(vulnerability_report_path),
    }
    value: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "release_id": f"{source_sha[:12]}-{image_digest[7:19]}",
        "service": SERVICE,
        "repository": REPOSITORY,
        "source": {
            "git_sha": source_sha,
            "git_tree_id": git_tree_id,
            "ref": SOURCE_REF,
        },
        "image": {
            "repository": IMAGE_REPOSITORY,
            "digest": image_digest,
            "reference": f"{IMAGE_REPOSITORY}@{image_digest}",
            "platforms": PLATFORMS,
            "base_image": BASE_IMAGE,
        },
        "runtime": {
            "schema_or_migration_head": _schema_head(root),
            "runtime_profile_ids": _runtime_profile_ids(root),
            "external_effects_default": "disabled",
        },
        "build": {
            "workflow_path": WORKFLOW_PATH,
            "workflow_identity": CERTIFICATE_IDENTITY,
            "run_id": run_id,
            "run_attempt": run_attempt,
            "built_at": built_at,
            "runner_image": "ubuntu-24.04",
            "provenance": "buildkit-mode-max",
        },
        "artifacts": artifacts,
        "verification": {
            "image_signature": "sigstore-keyless",
            "manifest_signature": "sigstore-keyless-bundle",
            "oidc_issuer": OIDC_ISSUER,
            "certificate_identity": CERTIFICATE_IDENTITY,
            "transparency_log_required": True,
            "vulnerability_policy": "fail-high-or-critical-with-fix",
        },
        "promotion": {
            "staging_and_production_same_digest": True,
            "mutable_tags_authoritative": False,
        },
    }
    validate_manifest(value)
    return value


def _expect_keys(value: object, expected: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ReleaseManifestError(f"{label} must be an object")
    if set(value) != expected:
        raise ReleaseManifestError(f"{label} fields do not match the v1 contract")
    return value


def _expect_constant(value: object, expected: object, label: str) -> None:
    if value != expected:
        raise ReleaseManifestError(f"{label} is not canonical")


def validate_manifest(
    value: object,
    *,
    expected_source_sha: str | None = None,
    expected_image_digest: str | None = None,
) -> dict[str, Any]:
    root = _expect_keys(
        value,
        {
            "schema_version",
            "release_id",
            "service",
            "repository",
            "source",
            "image",
            "runtime",
            "build",
            "artifacts",
            "verification",
            "promotion",
        },
        "manifest",
    )
    _expect_constant(root["schema_version"], SCHEMA_VERSION, "schema_version")
    _expect_constant(root["service"], SERVICE, "service")
    _expect_constant(root["repository"], REPOSITORY, "repository")

    source = _expect_keys(root["source"], {"git_sha", "git_tree_id", "ref"}, "source")
    if not isinstance(source["git_sha"], str) or SHA40.fullmatch(source["git_sha"]) is None:
        raise ReleaseManifestError("source.git_sha must be lowercase 40-character hex")
    if not isinstance(source["git_tree_id"], str) or SHA40.fullmatch(source["git_tree_id"]) is None:
        raise ReleaseManifestError("source.git_tree_id must be lowercase 40-character hex")
    _expect_constant(source["ref"], SOURCE_REF, "source.ref")
    if expected_source_sha is not None and source["git_sha"] != expected_source_sha.lower():
        raise ReleaseManifestError("manifest source SHA does not match the expected release")

    image = _expect_keys(
        root["image"],
        {"repository", "digest", "reference", "platforms", "base_image"},
        "image",
    )
    _expect_constant(image["repository"], IMAGE_REPOSITORY, "image.repository")
    if not isinstance(image["digest"], str) or DIGEST.fullmatch(image["digest"]) is None:
        raise ReleaseManifestError("image.digest must be an immutable sha256 digest")
    _expect_constant(
        image["reference"],
        f"{IMAGE_REPOSITORY}@{image['digest']}",
        "image.reference",
    )
    _expect_constant(image["platforms"], PLATFORMS, "image.platforms")
    _expect_constant(image["base_image"], BASE_IMAGE, "image.base_image")
    if expected_image_digest is not None and image["digest"] != expected_image_digest.lower():
        raise ReleaseManifestError("manifest image digest does not match the expected release")
    _expect_constant(
        root["release_id"],
        f"{source['git_sha'][:12]}-{image['digest'][7:19]}",
        "release_id",
    )

    runtime = _expect_keys(
        root["runtime"],
        {"schema_or_migration_head", "runtime_profile_ids", "external_effects_default"},
        "runtime",
    )
    if not isinstance(runtime["schema_or_migration_head"], str) or re.fullmatch(
        r"[0-9]{4}_[a-z0-9_]+", runtime["schema_or_migration_head"]
    ) is None:
        raise ReleaseManifestError("runtime migration head is invalid")
    _expect_constant(
        runtime["runtime_profile_ids"],
        [
            "codestra-middleware-production-compose-v1",
            "codestra-middleware-production-v1",
            "codestra-middleware-staging-v1",
        ],
        "runtime.runtime_profile_ids",
    )
    _expect_constant(
        runtime["external_effects_default"],
        "disabled",
        "runtime.external_effects_default",
    )

    build = _expect_keys(
        root["build"],
        {
            "workflow_path",
            "workflow_identity",
            "run_id",
            "run_attempt",
            "built_at",
            "runner_image",
            "provenance",
        },
        "build",
    )
    _expect_constant(build["workflow_path"], WORKFLOW_PATH, "build.workflow_path")
    _expect_constant(
        build["workflow_identity"], CERTIFICATE_IDENTITY, "build.workflow_identity"
    )
    if type(build["run_id"]) is not int or build["run_id"] < 1:
        raise ReleaseManifestError("build.run_id must be a positive integer")
    if type(build["run_attempt"]) is not int or build["run_attempt"] < 1:
        raise ReleaseManifestError("build.run_attempt must be a positive integer")
    if not isinstance(build["built_at"], str) or not build["built_at"].endswith("Z"):
        raise ReleaseManifestError("build.built_at must be an RFC3339 UTC timestamp")
    try:
        parsed_time = datetime.fromisoformat(build["built_at"].removesuffix("Z") + "+00:00")
    except ValueError as exc:
        raise ReleaseManifestError("build.built_at is not a valid timestamp") from exc
    if parsed_time.utcoffset() != timezone.utc.utcoffset(parsed_time):
        raise ReleaseManifestError("build.built_at must use UTC")
    _expect_constant(build["runner_image"], "ubuntu-24.04", "build.runner_image")
    _expect_constant(build["provenance"], "buildkit-mode-max", "build.provenance")

    artifacts = _expect_keys(
        root["artifacts"],
        {*FIXED_ARTIFACTS, "sbom", "vulnerability_report"},
        "artifacts",
    )
    for key, (_, expected_path) in FIXED_ARTIFACTS.items():
        entry = _expect_keys(artifacts[key], {"path", "sha256"}, f"artifacts.{key}")
        _expect_constant(entry["path"], expected_path, f"artifacts.{key}.path")
        if not isinstance(entry["sha256"], str) or DIGEST.fullmatch(entry["sha256"]) is None:
            raise ReleaseManifestError(f"artifacts.{key}.sha256 is invalid")
    for key in ("sbom", "vulnerability_report"):
        entry = _expect_keys(artifacts[key], {"path", "sha256"}, f"artifacts.{key}")
        if not isinstance(entry["path"], str) or EVIDENCE_NAME.fullmatch(entry["path"]) is None:
            raise ReleaseManifestError(f"artifacts.{key}.path is unsafe")
        if Path(entry["path"]).name != entry["path"]:
            raise ReleaseManifestError(f"artifacts.{key}.path must be a filename")
        if not isinstance(entry["sha256"], str) or DIGEST.fullmatch(entry["sha256"]) is None:
            raise ReleaseManifestError(f"artifacts.{key}.sha256 is invalid")

    verification = _expect_keys(
        root["verification"],
        {
            "image_signature",
            "manifest_signature",
            "oidc_issuer",
            "certificate_identity",
            "transparency_log_required",
            "vulnerability_policy",
        },
        "verification",
    )
    _expect_constant(verification["image_signature"], "sigstore-keyless", "image signature")
    _expect_constant(
        verification["manifest_signature"],
        "sigstore-keyless-bundle",
        "manifest signature",
    )
    _expect_constant(verification["oidc_issuer"], OIDC_ISSUER, "OIDC issuer")
    _expect_constant(
        verification["certificate_identity"], CERTIFICATE_IDENTITY, "certificate identity"
    )
    _expect_constant(
        verification["transparency_log_required"], True, "transparency log policy"
    )
    _expect_constant(
        verification["vulnerability_policy"],
        "fail-high-or-critical-with-fix",
        "vulnerability policy",
    )
    promotion = _expect_keys(
        root["promotion"],
        {"staging_and_production_same_digest", "mutable_tags_authoritative"},
        "promotion",
    )
    _expect_constant(
        promotion["staging_and_production_same_digest"], True, "same-digest promotion"
    )
    _expect_constant(
        promotion["mutable_tags_authoritative"], False, "mutable tag authority"
    )
    return root


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseManifestError("release manifest cannot be loaded") from exc
    validated = validate_manifest(value)
    if raw != canonical_json(validated):
        raise ReleaseManifestError("release manifest is not canonical JSON")
    return validated


def verify_workspace(
    value: dict[str, Any],
    *,
    root: Path,
    evidence_dir: Path,
) -> None:
    verify_source_checkout(
        root=root,
        expected_source_sha=value["source"]["git_sha"],
        expected_git_tree_id=value["source"]["git_tree_id"],
    )
    expected = _fixed_artifact_entries(root)
    artifacts = value["artifacts"]
    for key, entry in expected.items():
        if artifacts[key] != entry:
            raise ReleaseManifestError(f"workspace artifact digest mismatch: {key}")
    if value["runtime"]["schema_or_migration_head"] != _schema_head(root):
        raise ReleaseManifestError("workspace migration head does not match manifest")
    if value["runtime"]["runtime_profile_ids"] != _runtime_profile_ids(root):
        raise ReleaseManifestError("workspace runtime profiles do not match manifest")
    for key in ("sbom", "vulnerability_report"):
        evidence = evidence_dir / value["artifacts"][key]["path"]
        if sha256_file(evidence) != value["artifacts"][key]["sha256"]:
            raise ReleaseManifestError(f"release evidence digest mismatch: {key}")


def verify_sigstore_bundle(manifest: Path, bundle: Path, cosign: str) -> None:
    command = [
        cosign,
        "verify-blob",
        str(manifest),
        "--bundle",
        str(bundle),
        "--certificate-identity",
        CERTIFICATE_IDENTITY,
        "--certificate-oidc-issuer",
        OIDC_ISSUER,
    ]
    try:
        subprocess.run(command, check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ReleaseManifestError("Sigstore manifest verification failed") from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("--source-sha", required=True)
    create.add_argument("--git-tree-id", required=True)
    create.add_argument("--image-digest", required=True)
    create.add_argument("--built-at", required=True)
    create.add_argument("--run-id", required=True, type=int)
    create.add_argument("--run-attempt", required=True, type=int)
    create.add_argument("--sbom", required=True, type=Path)
    create.add_argument("--vulnerability-report", required=True, type=Path)
    create.add_argument("--output", required=True, type=Path)

    verify = commands.add_parser("verify")
    verify.add_argument("--manifest", required=True, type=Path)
    verify.add_argument("--bundle", type=Path)
    verify.add_argument("--expected-source-sha")
    verify.add_argument("--expected-image-digest")
    verify.add_argument("--skip-workspace", action="store_true")
    verify.add_argument("--workspace-root", type=Path, default=ROOT)
    verify.add_argument("--cosign", default="cosign")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "create":
            verify_source_checkout(
                root=ROOT,
                expected_source_sha=args.source_sha,
                expected_git_tree_id=args.git_tree_id,
            )
            manifest = build_manifest(
                root=ROOT,
                source_sha=args.source_sha,
                git_tree_id=args.git_tree_id,
                image_digest=args.image_digest,
                built_at=args.built_at,
                run_id=args.run_id,
                run_attempt=args.run_attempt,
                sbom_path=args.sbom,
                vulnerability_report_path=args.vulnerability_report,
            )
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_bytes(canonical_json(manifest))
            print(f"RELEASE_MANIFEST_CREATED={args.output}")
            return 0

        manifest = load_manifest(args.manifest)
        validate_manifest(
            manifest,
            expected_source_sha=args.expected_source_sha,
            expected_image_digest=args.expected_image_digest,
        )
        if not args.skip_workspace:
            verify_workspace(
                manifest,
                root=args.workspace_root.resolve(),
                evidence_dir=args.manifest.resolve().parent,
            )
        if args.bundle is not None:
            verify_sigstore_bundle(args.manifest, args.bundle, args.cosign)
        print(
            "SIGNED_RELEASE_MANIFEST=PASS "
            f"RELEASE_ID={manifest['release_id']} "
            f"IMAGE={manifest['image']['reference']}"
        )
        return 0
    except ReleaseManifestError as exc:
        print(f"RELEASE_MANIFEST_INVALID={exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
