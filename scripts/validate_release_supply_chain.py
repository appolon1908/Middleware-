#!/usr/bin/env python3
from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_BASE = (
    "python:3.13.15-slim-bookworm@"
    "sha256:00faa2debb87529f9f0764e9491d8ba400a3678976616c3bd7cb193745ac20d1"
)
TEST_BASE = (
    "python:3.13.15-bookworm@"
    "sha256:62eafe52c91cad83c2c74e630bfde917da8c253673e695665d454def84fc9a13"
)
FINAL_BASE = (
    "gcr.io/distroless/python3-debian13:nonroot@"
    "sha256:f3d5ddc6c64a019fe520e7f005f2880be21e6afc461b10a3c15ef2e4edc71e33"
)
REQUIRED = (
    "requirements-runtime.in",
    "requirements-runtime.txt",
    "requirements-test.in",
    "requirements-test.txt",
    "requirements-connector-runtime.in",
    "requirements-connector-runtime.txt",
    "contracts/release-manifest.v1.schema.json",
    "contracts/runtime-safety-readback.v1.schema.json",
    "contracts/runtime-safety-readback.v1.1.schema.json",
    "scripts/release_manifest.py",
    "scripts/staging_synthetic_acceptance.py",
    "scripts/synthetic_acceptance_ci.sh",
    "tests/integration/test_synthetic_acceptance.py",
    ".github/workflows/release.yml",
)


def require(text: str, fragment: str, label: str, errors: list[str]) -> None:
    if fragment not in text:
        errors.append(f"missing {label}: {fragment}")


def validate_lock(path: Path, errors: list[str]) -> dict[str, str]:
    packages: dict[str, str] = {}
    current_has_hash = False
    current_name: str | None = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("--hash=sha256:"):
            current_has_hash = True
            continue
        if current_name is not None and not current_has_hash:
            errors.append(f"{path.name} entry lacks a SHA-256 hash: {current_name}")
        match = re.match(r"^([A-Za-z0-9_.-]+)==([^ ;\\]+)", line)
        if match is None:
            errors.append(f"{path.name} has an unpinned requirement: {line[:80]}")
            current_name = None
            current_has_hash = False
            continue
        current_name = match.group(1).lower().replace("_", "-")
        packages[current_name] = match.group(2)
        current_has_hash = "--hash=sha256:" in line
    if current_name is not None and not current_has_hash:
        errors.append(f"{path.name} entry lacks a SHA-256 hash: {current_name}")
    return packages


def direct_requirements(path: Path, errors: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "-r ")):
            continue
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)(?:\[[A-Za-z0-9,_.-]+\])?==([^\s]+)", line)
        if match is None:
            errors.append(f"{path.name} direct requirement is not exact: {line}")
            continue
        result[match.group(1).lower().replace("_", "-")] = match.group(2)
    return result


def main() -> int:
    errors: list[str] = []
    for relative in REQUIRED:
        if not (ROOT / relative).is_file():
            errors.append(f"required release control is missing: {relative}")
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1

    runtime_packages = validate_lock(ROOT / "requirements-runtime.txt", errors)
    test_packages = validate_lock(ROOT / "requirements-test.txt", errors)
    connector_packages = validate_lock(
        ROOT / "requirements-connector-runtime.txt", errors
    )
    runtime_direct = direct_requirements(ROOT / "requirements-runtime.in", errors)
    test_direct = direct_requirements(ROOT / "requirements-test.in", errors)
    connector_direct = direct_requirements(
        ROOT / "requirements-connector-runtime.in", errors
    )
    for package, version in {**runtime_direct, **test_direct}.items():
        if test_packages.get(package) != version:
            errors.append(f"test lock does not bind {package}=={version}")
    for package, version in runtime_direct.items():
        if runtime_packages.get(package) != version:
            errors.append(f"runtime lock does not bind {package}=={version}")
    for package in ("fastapi", "asyncpg", "redis", "nats-py", "temporalio", "prometheus-client"):
        if package not in runtime_packages:
            errors.append(f"runtime lock is missing {package}")
    if {"pytest", "pytest-asyncio"} & set(runtime_packages):
        errors.append("production runtime lock contains test tooling")
    if not {"pytest", "pytest-asyncio"}.issubset(test_packages):
        errors.append("test lock is missing pytest tooling")
    for package, version in connector_direct.items():
        if connector_packages.get(package) != version:
            errors.append(f"connector lock does not bind {package}=={version}")
    if {"pytest", "pytest-asyncio"} & set(connector_packages):
        errors.append("connector production lock contains test tooling")

    dockerfile = (ROOT / "Dockerfile.runtime").read_text(encoding="utf-8")
    require(dockerfile, f"ARG RUNTIME_BASE={RUNTIME_BASE}", "digest-pinned runtime base", errors)
    require(dockerfile, f"ARG TEST_BASE={TEST_BASE}", "digest-pinned test base", errors)
    require(dockerfile, f"ARG FINAL_BASE={FINAL_BASE}", "digest-pinned final base", errors)
    require(dockerfile, "--require-hashes", "hashed dependency install", errors)
    for target in ("runtime", "worker", "connector-runtime", "test"):
        require(dockerfile, f" AS {target}", f"supported {target} target", errors)
    for label in (
        "org.opencontainers.image.source",
        "org.opencontainers.image.revision",
        "org.opencontainers.image.version",
        "org.opencontainers.image.created",
    ):
        require(dockerfile, label, f"OCI label {label}", errors)
    if "apt-get upgrade" in dockerfile or "apk upgrade" in dockerfile:
        errors.append("Dockerfile performs an uncontrolled OS package upgrade")
    if "openssl=3.5.7-r0" in dockerfile:
        errors.append("obsolete captured OpenSSL package pin returned")
    require(
        dockerfile,
        "connectors ./connectors",
        "generated command registry bundle",
        errors,
    )
    require(dockerfile, "migrations ./migrations", "migration bundle", errors)
    require(
        dockerfile,
        "scripts/migrate_runtime.py ./scripts/migrate_runtime.py",
        "migration runner",
        errors,
    )

    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    checks = {
        "workflow_run:": "post-CI release trigger",
        "branches: [main]": "main-only release",
        "target: runtime": "production runtime build target",
        "packages: write": "GHCR write permission",
        "id-token: write": "keyless signing permission",
        "provenance: mode=max": "maximum build provenance",
        "sbom: true": "BuildKit SBOM attestation",
        "severity-cutoff: high": "high vulnerability gate",
        "only-fixed: true": "actionable vulnerability gate",
        "cosign sign --yes": "image signature",
        '--annotations "codestra.source_sha=$RELEASE_SOURCE_SHA"': "source annotation",
        '--annotations "codestra.schema_head=0009_observability_incidents"': "schema annotation",
        "cosign attest --yes": "SBOM attestation",
        "cosign sign-blob --yes": "manifest signature",
        "cosign verify-attestation": "SBOM attestation verification",
        "scripts/release_manifest.py verify": "manifest verification",
        "certificate-identity": "exact signer identity verification",
        "certificate-oidc-issuer": "OIDC issuer verification",
        "cancel-in-progress: false": "non-cancellable release evidence",
    }
    for fragment, label in checks.items():
        require(workflow, fragment, label, errors)
    if re.search(r"(?:^|[:/@])latest(?:$|\s)", workflow, re.MULTILINE):
        errors.append("release workflow must not reference a latest tag")
    if "pull_request:" in workflow:
        errors.append("release workflow must never publish from pull_request")

    release_manifest = (ROOT / "scripts/release_manifest.py").read_text(
        encoding="utf-8"
    )
    require(
        release_manifest,
        FINAL_BASE,
        "release-manifest final base-image identity",
        errors,
    )
    release_schema = (
        ROOT / "contracts/release-manifest.v1.schema.json"
    ).read_text(encoding="utf-8")
    require(
        release_schema,
        FINAL_BASE,
        "release-manifest schema final base-image identity",
        errors,
    )

    middleware_ci = (ROOT / ".github/workflows/middleware-ci.yml").read_text(
        encoding="utf-8"
    )
    require(
        middleware_ci,
        "docker-runtime-build",
        "pre-release runtime image smoke test",
        errors,
    )
    for job in (
        "docker-test-build",
        "connector-runtime-build",
        "container-security",
    ):
        require(middleware_ci, job, f"required CI job {job}", errors)
    require(
        middleware_ci,
        "postgres@sha256:cf78e76683b9ca8c5733cbbdce6c9262b45b6767934dd0a95e671f9a0fc20685",
        "digest-pinned PostgreSQL CI service",
        errors,
    )
    require(
        middleware_ci,
        "redis@sha256:ff02b58f971e7d7d156a1267e283fcbbeee91773b6aa36c49dac28ecfe28eadf",
        "digest-pinned Redis CI service",
        errors,
    )
    require(
        middleware_ci,
        "synthetic-acceptance-e2e:",
        "combined synthetic acceptance job",
        errors,
    )
    require(
        middleware_ci,
        "bash scripts/synthetic_acceptance_ci.sh",
        "combined synthetic acceptance runner",
        errors,
    )

    if errors:
        print("Release supply-chain validation failed:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    print(
        "RELEASE_SUPPLY_CHAIN=PASS "
        f"RUNTIME_PACKAGES={len(runtime_packages)} "
        f"TEST_PACKAGES={len(test_packages)} "
        f"CONNECTOR_PACKAGES={len(connector_packages)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
