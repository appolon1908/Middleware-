"""Exact-run evidence must never inherit a previous self-hosted workspace."""

from __future__ import annotations

import ast
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import pytest
import yaml

from scripts.required_ci_evidence import REQUIRED_REPORTS, REQUIRED_STEPS


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/required-ci.yml"
REPORTS = (
    "checked-out-identity.txt python-version.txt dependency-check.txt "
    "environment-names-only.txt ruff.txt ruff-changed.txt mypy.txt "
    "docker-test-build.txt migration-upgrade.txt pytest-full.txt "
    "campaign-extension-concurrency.txt campaign-identity-concurrency.txt "
    "runtime-migration.txt runtime-migration-verify.txt middleware-rehearsal.dump "
    "restoration.sha256 application-startup.txt healthz-final.json readyz-final.json "
    "readiness-healthy.json readiness-wrong-credential.json readiness-dns-failure.json "
    "readiness-tcp-failure.json readiness-redis-failure.json readiness-keycloak-failure.json "
    "release-endpoint-contract.txt"
).split()


def steps() -> list[dict]:
    return yaml.safe_load(WORKFLOW.read_text())["jobs"]["test"]["steps"]


def step_named(name: str) -> dict:
    return next(step for step in steps() if step.get("name") == name)


def permits_upload(condition: str, outcomes: dict[str, str], failed: bool) -> bool:
    """Evaluate the small boolean/status subset used by this workflow's guards."""
    expression = condition.removeprefix("${{").removesuffix("}}").strip()
    expression = expression.replace("always()", "True")
    expression = expression.replace("success()", str(not failed))
    expression = expression.replace("failure()", str(failed))
    expression = re.sub(
        r"steps\.([a-z_]+)\.outcome",
        lambda match: repr(outcomes.get(match.group(1), "skipped")), expression,
    )
    expression = expression.replace("&&", " and ").replace("||", " or ")
    parsed = ast.parse(expression, mode="eval")
    for node in ast.walk(parsed):
        assert isinstance(node, (
            ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.Compare,
            ast.Eq, ast.NotEq, ast.Constant,
        )), f"Unsupported guard syntax: {type(node).__name__}"
    return bool(eval(compile(parsed, "<workflow guard>", "eval"), {"__builtins__": {}}))


@pytest.mark.parametrize("manifest", ["skipped", "failure"])
def test_initialization_failure_cannot_upload_stale_root_reports(
    tmp_path: Path, manifest: str,
) -> None:
    (tmp_path / "checked-out-identity.txt").write_text("old-sha old-tree old-parent\n")
    (tmp_path / "pytest-full.txt").write_text("999 passed in an earlier run\n")
    upload = step_named("Upload exact-SHA CI evidence")
    assert not permits_upload(
        upload["if"],
        {"checkout": "skipped", "evidence_init": "skipped", "evidence_manifest": manifest},
        failed=True,
    )


def test_manifest_rejects_stale_root_reports_without_current_initialization(tmp_path: Path) -> None:
    for name in REPORTS:
        (tmp_path / name).write_text("stale previous-run output\n")
    environment = {
        "PATH": os.pathsep.join((str(Path(sys.executable).parent), "/usr/bin", "/bin")),
        "TARGET_SHA": "b" * 40,
        "GITHUB_RUN_ID": "123",
        "GITHUB_RUN_ATTEMPT": "2",
        "GITHUB_REPOSITORY": "example/middleware",
        "GITHUB_JOB": "test",
        "RUNNER_TEMP": str(tmp_path / "runner-temp"),
    }
    script = ROOT / "scripts/required_ci_evidence.py"
    if script.exists():
        (tmp_path / "scripts").mkdir()
        (tmp_path / "scripts/required_ci_evidence.py").write_bytes(script.read_bytes())
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", step_named("Create test evidence manifest")["run"]],
        cwd=tmp_path, env=environment, capture_output=True, text=True, check=False,
    )
    assert result.returncode != 0, "Stale workspace reports must not certify a new run/attempt"
    assert not (tmp_path / "required-ci-evidence.sha256").exists()


@pytest.fixture
def attempt(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "source.txt").write_text("source under test\n")
    environment = {
        "PATH": os.pathsep.join((str(Path(sys.executable).parent), "/usr/bin", "/bin")),
        "GITHUB_REPOSITORY": "example/middleware", "GITHUB_RUN_ID": "123",
        "GITHUB_RUN_ATTEMPT": "2", "GITHUB_JOB": "test",
        "RUNNER_TEMP": str(tmp_path / "runner-temp"),
        "GITHUB_ENV": str(tmp_path / "github-env"),
        "GITHUB_OUTPUT": str(tmp_path / "github-output"),
        "REQUIRED_CI_STEP_RESULTS": json.dumps({
            name: {"outcome": "success"} for name in REQUIRED_STEPS
        }),
    }
    Path(environment["RUNNER_TEMP"]).mkdir()
    for args in (
        ["init", "-q"], ["add", "source.txt"],
        ["-c", "user.name=Evidence Test", "-c", "user.email=evidence@example.invalid",
         "-c", "commit.gpgsign=false", "commit", "-qm", "Fixture source"],
    ):
        subprocess.run(["git", *args], cwd=repository, env=environment, check=True, capture_output=True)
    environment["TARGET_SHA"] = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repository, env=environment, text=True,
    ).strip()
    return repository, environment


def invoke(attempt: tuple[Path, dict[str, str]], command: str) -> subprocess.CompletedProcess[str]:
    repository, environment = attempt
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts/required_ci_evidence.py"), command],
        cwd=repository, env=environment, capture_output=True, text=True, check=False,
    )


def initialize(attempt: tuple[Path, dict[str, str]]) -> Path:
    result = invoke(attempt, "init")
    assert result.returncode == 0, result.stderr
    _, environment = attempt
    values = dict(line.split("=", 1) for line in Path(environment["GITHUB_ENV"]).read_text().splitlines())
    environment.update(values)
    return Path(environment["REQUIRED_CI_EVIDENCE_DIR"])


def populate_reports(directory: Path) -> None:
    for name in REQUIRED_REPORTS:
        if not (directory / name).exists():
            (directory / name).write_text("fresh synthetic test fixture\n")


def test_complete_evidence_binds_actual_source_tree_run_attempt_and_every_report(attempt) -> None:
    repository, environment = attempt
    directory = initialize(attempt)
    populate_reports(directory)
    (directory / "readiness-healthy.log").write_text("also retain runtime diagnostics\n")
    result = invoke(attempt, "complete")
    assert result.returncode == 0, result.stderr
    manifest = json.loads((directory / "required-ci-evidence.json").read_text())
    assert manifest["status"] == "success"
    assert manifest["purpose"] == "complete-test-evidence"
    assert manifest["target_sha"] == manifest["checked_out_sha"] == environment["TARGET_SHA"]
    assert manifest["tree_sha"] == subprocess.check_output(
        ["git", "rev-parse", "HEAD^{tree}"], cwd=repository, text=True,
    ).strip()
    assert (manifest["repository"], manifest["run_id"], manifest["run_attempt"], manifest["job"]) == (
        "example/middleware", "123", "2", "test",
    )
    assert manifest["missing_reports"] == []
    assert all(outcome == "success" for outcome in manifest["step_outcomes"].values())
    report_files = {path.name for path in directory.iterdir()} - {
        "required-ci-evidence.json", "required-ci-evidence.sha256",
    }
    assert set(manifest["files"]) == report_files
    for name, digest in manifest["files"].items():
        assert hashlib.sha256((directory / name).read_bytes()).hexdigest() == digest
    checksums = subprocess.run(
        ["sha256sum", "--check", "required-ci-evidence.sha256"], cwd=directory,
        capture_output=True, text=True, check=False,
    )
    assert checksums.returncode == 0, checksums.stdout + checksums.stderr


def test_stale_root_reports_are_not_collected_or_counted_as_current_evidence(attempt) -> None:
    repository, _ = attempt
    for name in REQUIRED_REPORTS:
        (repository / name).write_text("old successful run\n")
    directory = initialize(attempt)
    result = invoke(attempt, "complete")
    assert result.returncode != 0
    assert "reports are missing" in result.stderr
    assert not (directory / "required-ci-evidence.json").exists()
    result = invoke(attempt, "diagnostics")
    assert result.returncode == 0, result.stderr
    manifest = json.loads((directory / "required-ci-diagnostics.json").read_text())
    assert manifest["status"] == "incomplete"
    assert manifest["purpose"] == "failure-diagnostics"
    assert "pytest-full.txt" in manifest["missing_reports"]
    assert "pytest-full.txt" not in manifest["files"]


@pytest.mark.parametrize("outcome", ["failure", "cancelled", "skipped", "", "unknown"])
@pytest.mark.parametrize("producer", ["database_tests", "egress_install", "egress_apply", "egress_check"])
def test_failed_or_missing_test_execution_cannot_produce_passing_evidence(attempt, outcome: str, producer: str) -> None:
    _, environment = attempt
    directory = initialize(attempt)
    populate_reports(directory)
    states = json.loads(environment["REQUIRED_CI_STEP_RESULTS"])
    states[producer] = {"outcome": outcome}
    environment["REQUIRED_CI_STEP_RESULTS"] = json.dumps(states)
    result = invoke(attempt, "complete")
    assert result.returncode != 0
    assert not (directory / "required-ci-evidence.json").exists()
    diagnostic = invoke(attempt, "diagnostics")
    assert diagnostic.returncode == 0, diagnostic.stderr
    manifest = json.loads((directory / "required-ci-diagnostics.json").read_text())
    assert manifest["status"] == "incomplete"
    assert manifest["step_outcomes"][producer] == outcome


@pytest.mark.parametrize("field", ["repository", "target_sha", "tree_sha", "run_id", "run_attempt", "job"])
def test_mismatched_recorded_identity_fails_closed(attempt, field: str) -> None:
    directory = initialize(attempt)
    populate_reports(directory)
    path = directory / "attempt-identity.json"
    identity = json.loads(path.read_text())
    identity[field] = "old-attempt-value"
    path.write_text(json.dumps(identity))
    for command in ("complete", "diagnostics"):
        assert invoke(attempt, command).returncode != 0


def test_reinitializing_existing_attempt_fails_without_touching_its_reports(attempt) -> None:
    directory = initialize(attempt)
    report = directory / "pytest-full.txt"
    report.write_text("preserve existing evidence\n")
    assert invoke(attempt, "init").returncode != 0
    assert report.read_text() == "preserve existing evidence\n"


def test_another_attempt_cannot_reuse_previous_attempt_directory(attempt) -> None:
    directory = initialize(attempt)
    populate_reports(directory)
    attempt[1]["GITHUB_RUN_ATTEMPT"] = "3"
    assert invoke(attempt, "complete").returncode != 0
    assert invoke(attempt, "diagnostics").returncode != 0


def test_symlink_to_previous_workspace_report_is_rejected(attempt) -> None:
    repository, _ = attempt
    directory = initialize(attempt)
    populate_reports(directory)
    old = repository / "old-report.txt"
    old.write_text("old report\n")
    report = directory / "pytest-full.txt"
    report.unlink()
    report.symlink_to(old)
    assert invoke(attempt, "complete").returncode != 0
    assert invoke(attempt, "diagnostics").returncode != 0


def test_hidden_reports_cannot_escape_the_uploaded_file_set(attempt) -> None:
    directory = initialize(attempt)
    populate_reports(directory)
    (directory / ".hidden-report").write_text("upload-artifact excludes hidden files\n")
    assert invoke(attempt, "complete").returncode != 0


def test_workflow_wires_all_gates_and_separates_incomplete_diagnostics() -> None:
    by_id = {step["id"]: step for step in steps() if "id" in step}
    assert set(REQUIRED_STEPS) <= by_id.keys()
    complete = step_named("Upload exact-SHA CI evidence")
    diagnostics = step_named("Upload current-attempt failure diagnostics")
    assert complete["with"]["path"] == diagnostics["with"]["path"] == "${{ steps.evidence_init.outputs.directory }}"
    assert "github.run_attempt" in complete["with"]["name"]
    assert "github.run_attempt" in diagnostics["with"]["name"]
    assert diagnostics["with"]["name"].startswith("required-ci-diagnostics-")
    outcomes = {"evidence_manifest": "failure", "evidence_diagnostics": "success"}
    assert not permits_upload(complete["if"], outcomes, failed=True)
    assert permits_upload(diagnostics["if"], outcomes, failed=True)
    outcomes = {"evidence_manifest": "success", "evidence_diagnostics": "skipped"}
    assert permits_upload(complete["if"], outcomes, failed=False)
    assert not permits_upload(diagnostics["if"], outcomes, failed=False)
    outcomes = {"evidence_manifest": "skipped", "evidence_diagnostics": "skipped"}
    assert not permits_upload(complete["if"], outcomes, failed=True)
    assert not permits_upload(diagnostics["if"], outcomes, failed=True)
