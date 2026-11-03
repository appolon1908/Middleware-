"""Bind native CI reports to one verified source checkout and workflow attempt."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys


REQUIRED_STEPS = (
    "checkout", "verify_checkout", "evidence_init", "runner_resources",
    "egress_install", "egress_apply", "egress_check", "python", "dependencies",
    "isolation", "lint", "docker_build", "database_tests", "rollback", "startup", "readiness", "runner_resources_final",
)
REQUIRED_REPORTS = (
    "checked-out-identity.txt", "python-version.txt", "dependency-check.txt",
    "runner-resources-initial.txt", "runner-resources-final.txt",
    "egress-guard-install.txt", "egress-guard-apply.txt", "egress-guard-check.txt",
    "environment-names-only.txt", "ruff.txt", "ruff-changed.txt", "mypy.txt",
    "docker-test-build.txt", "migration-upgrade.txt", "pytest-full.txt",
    "campaign-extension-concurrency.txt", "campaign-identity-concurrency.txt",
    "runtime-migration.txt", "runtime-migration-verify.txt", "middleware-rehearsal.dump",
    "restoration.sha256", "application-startup.txt", "healthz-final.json", "readyz-final.json",
    "readiness-healthy.json", "readiness-wrong-credential.json", "readiness-dns-failure.json",
    "readiness-tcp-failure.json", "readiness-redis-failure.json", "readiness-keycloak-failure.json",
    "release-endpoint-contract.txt",
)
IDENTITY_FILE = "attempt-identity.json"
COMPLETE_MANIFEST = "required-ci-evidence.json"
DIAGNOSTIC_MANIFEST = "required-ci-diagnostics.json"
CHECKSUMS = "required-ci-evidence.sha256"


def git(*arguments: str) -> str:
    return subprocess.check_output(["git", *arguments], text=True).strip()


def current_identity() -> dict[str, str]:
    patterns = {
        "TARGET_SHA": r"[0-9a-f]{40}",
        "GITHUB_REPOSITORY": r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+",
        "GITHUB_RUN_ID": r"[1-9][0-9]*",
        "GITHUB_RUN_ATTEMPT": r"[1-9][0-9]*",
        "GITHUB_JOB": r"[A-Za-z0-9_-]+",
    }
    for name, pattern in patterns.items():
        if re.fullmatch(pattern, os.environ.get(name, "")) is None:
            raise ValueError(f"Missing or invalid {name}")
    sha = git("rev-parse", "HEAD")
    if sha != os.environ["TARGET_SHA"]:
        raise ValueError("Checked-out commit does not match TARGET_SHA")
    return {
        "repository": os.environ["GITHUB_REPOSITORY"],
        "target_sha": os.environ["TARGET_SHA"],
        "checked_out_sha": sha,
        "tree_sha": git("rev-parse", "HEAD^{tree}"),
        "run_id": os.environ["GITHUB_RUN_ID"],
        "run_attempt": os.environ["GITHUB_RUN_ATTEMPT"],
        "job": os.environ["GITHUB_JOB"],
    }


def expected_directory(identity: dict[str, str]) -> Path:
    runner_temp = os.environ.get("RUNNER_TEMP", "")
    if not runner_temp or not Path(runner_temp).is_absolute():
        raise ValueError("RUNNER_TEMP must be an absolute directory")
    name = "required-ci-" + "-".join((
        identity["repository"].replace("/", "_"), identity["run_id"],
        identity["run_attempt"], identity["job"], identity["target_sha"],
    ))
    return Path(runner_temp).resolve() / name


def write_json(path: Path, document: dict) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(document, stream, indent=2, sort_keys=True)
        stream.write("\n")


def initialize() -> None:
    identity = current_identity()
    directory = expected_directory(identity)
    # A repeated or concurrent initialization must fail rather than reuse data.
    directory.mkdir(mode=0o700)
    write_json(directory / IDENTITY_FILE, {
        **identity, "schema_version": 1,
        "initialized_at": datetime.now(timezone.utc).isoformat(),
    })
    (directory / "checked-out-identity.txt").write_text(
        git("show", "-s", "--format=%H %T %P", "HEAD") + "\n", encoding="utf-8",
    )
    for variable, entry in (("GITHUB_ENV", "REQUIRED_CI_EVIDENCE_DIR"), ("GITHUB_OUTPUT", "directory")):
        with Path(os.environ[variable]).open("a", encoding="utf-8") as stream:
            stream.write(f"{entry}={directory}\n")


def verified_directory(identity: dict[str, str]) -> Path:
    configured = os.environ.get("REQUIRED_CI_EVIDENCE_DIR", "")
    if not configured:
        raise ValueError("Current-attempt evidence was not initialized")
    directory = Path(configured)
    if directory.is_symlink() or directory != expected_directory(identity):
        raise ValueError("Evidence directory is not bound to this attempt")
    path = directory / IDENTITY_FILE
    if path.is_symlink() or not path.is_file():
        raise ValueError("Current-attempt identity is missing or unsafe")
    recorded = json.loads(path.read_text(encoding="utf-8"))
    if any(recorded.get(key) != value for key, value in identity.items()):
        raise ValueError("Evidence identity does not match the current source/run/attempt")
    return directory


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def create_manifest(*, complete: bool) -> None:
    identity = current_identity()
    directory = verified_directory(identity)
    steps = json.loads(os.environ.get("REQUIRED_CI_STEP_RESULTS", "{}"))
    if not isinstance(steps, dict):
        raise ValueError("Step outcomes must be an object")
    outcomes = {
        name: steps.get(name, {}).get("outcome", "skipped")
        for name in REQUIRED_STEPS
    }
    if complete and any(value != "success" for value in outcomes.values()):
        raise ValueError("Every required product step must succeed before complete evidence")
    files: dict[str, str] = {}
    for path in sorted(directory.iterdir()):
        if path.is_symlink() or not path.is_file() or path.name.startswith("."):
            raise ValueError(f"Unsafe evidence entry: {path.name}")
        if path.name in (COMPLETE_MANIFEST, DIAGNOSTIC_MANIFEST, CHECKSUMS):
            raise ValueError("A manifest already exists for this evidence directory")
        files[path.name] = file_digest(path)
    missing = sorted(set(REQUIRED_REPORTS) - files.keys())
    if complete and missing:
        raise ValueError("Required current-attempt reports are missing: " + ", ".join(missing))
    name = COMPLETE_MANIFEST if complete else DIAGNOSTIC_MANIFEST
    write_json(directory / name, {
        **identity, "schema_version": 1,
        "status": "success" if complete else "incomplete",
        "purpose": "complete-test-evidence" if complete else "failure-diagnostics",
        "step_outcomes": outcomes, "missing_reports": missing, "files": files,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    })
    files[name] = file_digest(directory / name)
    with (directory / CHECKSUMS).open("x", encoding="utf-8") as stream:
        for filename, digest in sorted(files.items()):
            stream.write(f"{digest}  {filename}\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("init", "complete", "diagnostics"))
    args = parser.parse_args()
    try:
        if args.command == "init":
            initialize()
        else:
            create_manifest(complete=args.command == "complete")
    except (KeyError, OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"required-ci evidence: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
