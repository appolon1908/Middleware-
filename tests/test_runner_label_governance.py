"""Runner-label governance for the persistent self-hosted Middleware runner.

The governed host (labels ``self-hosted, Linux, X64, middleware-ci``) is a
persistent operator machine, not an ephemeral VM. Under decision D1 trusted
events run on it and pull requests from forks fall back to a GitHub-hosted
runner; a small, named set of jobs is pinned to it by exact labels. Signing,
publishing and release-provenance workflows never run on it.

``tests/test_ci_isolation.py`` pins the fork-aware selector text, dynamic
service ports and the required-ci egress guard; this module pins the job set
and the workflow-wide invariants that no single workflow test can see.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github/workflows"
MIDDLEWARE_CI = WORKFLOWS / "middleware-ci.yml"

GOVERNED_LABELS = ["self-hosted", "Linux", "X64", "middleware-ci"]
FORK_AWARE_SELECTOR = (
    "${{ github.event_name == 'pull_request' && "
    "github.event.pull_request.head.repo.full_name != github.repository && "
    "'ubuntu-24.04' || fromJSON('[\"self-hosted\",\"Linux\",\"X64\",\"middleware-ci\"]') }}"
)
# Jobs pinned to the governed host by exact labels, each for a stated reason:
# pull_request_target gates are byte-pinned base-branch code; required-ci owns
# the host-only egress guard; the single-lane gate materializes branch refs.
EXACT_LABEL_JOBS = {
    ("production-orchestrator-contract.yml", "validate"): GOVERNED_LABELS,
    ("trusted-production-orchestrator-gate.yml", "validate-candidate"): GOVERNED_LABELS,
    ("required-ci.yml", "test"): GOVERNED_LABELS,
    ("required-ci.yml", "publish-final-status"): GOVERNED_LABELS,
    ("single-lane-agent-governance.yml", "governance"): ["self-hosted", "Linux", "X64", "ubuntu-24.04", "middleware-ci"],
}
# Branch-protection contexts plus the push-only main gate: rerouting must not
# rename, drop or add a job.
MIDDLEWARE_CI_JOB_NAMES = {
    "source-head-validation": "Validate middleware source head",
    "merge-result-validation": "Validate middleware merge result",
    "main-validation": "Validate middleware main push",
    "docker-runtime-build": "docker-runtime-build",
    "docker-test-build": "docker-test-build",
    "connector-runtime-build": "connector-runtime-build",
    "container-security": "container-security",
    "runtime-integration": "Disposable PostgreSQL Redis integration",
    "nats-jetstream-integration": "Disposable NATS JetStream integration",
    "temporal-workflow-integration": "Temporal critical workflow integration",
    "synthetic-acceptance-e2e": "Synthetic no-effect acceptance E2E",
    "required-validation": "validate",
}
SIGNING_OR_PUBLISHING_MARKERS = ("cosign sign", "id-token: write", "packages: write")


def load(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def triggers(workflow: dict[str, Any]) -> Any:
    # PyYAML resolves the bare ``on`` key to boolean True.
    return workflow.get("on", workflow.get(True))


def targets_governed_host(runs_on: Any) -> bool:
    text = json.dumps(runs_on)
    return "self-hosted" in text or "middleware-ci" in text


def all_jobs() -> list[tuple[str, str, dict[str, Any]]]:
    jobs = []
    for path in sorted(WORKFLOWS.glob("*.y*ml")):
        for job_id, job in (load(path).get("jobs") or {}).items():
            jobs.append((path.name, job_id, job))
    return jobs


def test_middleware_ci_job_set_and_required_context_names_are_stable() -> None:
    jobs = load(MIDDLEWARE_CI)["jobs"]
    assert {job_id: job["name"] for job_id, job in jobs.items()} == MIDDLEWARE_CI_JOB_NAMES
    for job_id, job in jobs.items():
        assert job["runs-on"] == FORK_AWARE_SELECTOR, job_id


def test_required_aggregate_still_fails_closed_on_every_gate() -> None:
    job = load(MIDDLEWARE_CI)["jobs"]["required-validation"]
    assert job["if"] == "always()"
    assert set(job["needs"]) == set(MIDDLEWARE_CI_JOB_NAMES) - {"required-validation"}
    script = job["steps"][0]["run"]
    assert 'test "$result" = success' in script
    assert 'test "$MAIN_RESULT" = skipped' in script
    assert 'test "$MAIN_RESULT" = success' in script


def test_middleware_ci_only_runs_for_trusted_event_types() -> None:
    on = triggers(load(MIDDLEWARE_CI))
    assert set(on) == {"pull_request", "push"}
    assert on["push"] == {"branches": ["main"]}


@pytest.mark.parametrize(
    ("workflow", "job_id", "job"),
    [pytest.param(*entry, id=f"{entry[0]}:{entry[1]}") for entry in all_jobs()],
)
def test_governed_host_is_reached_only_through_approved_selectors(
    workflow: str, job_id: str, job: dict[str, Any]
) -> None:
    runs_on = job.get("runs-on")
    if not targets_governed_host(runs_on):
        return
    if (workflow, job_id) in EXACT_LABEL_JOBS:
        assert runs_on == EXACT_LABEL_JOBS[(workflow, job_id)], f"{workflow}:{job_id}"
    else:
        assert runs_on == FORK_AWARE_SELECTOR, f"{workflow}:{job_id} drifted from the fork-aware selector"


@pytest.mark.parametrize(
    ("workflow", "job_id", "job"),
    [pytest.param(*entry, id=f"{entry[0]}:{entry[1]}") for entry in all_jobs()],
)
def test_governed_host_jobs_never_persist_checkout_credentials(
    workflow: str, job_id: str, job: dict[str, Any]
) -> None:
    if not targets_governed_host(job.get("runs-on")):
        return
    for step in job.get("steps") or []:
        if str(step.get("uses", "")).startswith("actions/checkout@"):
            assert (step.get("with") or {}).get("persist-credentials") is False, f"{workflow}:{job_id}"


def test_signing_and_publishing_workflows_never_use_the_governed_host() -> None:
    # Release provenance is verified with --deny-self-hosted-runners.
    for path in sorted(WORKFLOWS.glob("*.y*ml")):
        source = path.read_text(encoding="utf-8")
        if not any(marker in source for marker in SIGNING_OR_PUBLISHING_MARKERS):
            continue
        for job_id, job in (load(path).get("jobs") or {}).items():
            assert not targets_governed_host(job.get("runs-on")), f"{path.name}:{job_id}"


def test_middleware_ci_keeps_least_privilege_and_no_host_mutation() -> None:
    workflow = load(MIDDLEWARE_CI)
    assert workflow["permissions"] == {"contents": "read"}
    for job_id, job in workflow["jobs"].items():
        assert "permissions" not in job, job_id
    source = MIDDLEWARE_CI.read_text(encoding="utf-8")
    for forbidden in ("sudo", "iptables", "127.0.0.1:5432", "127.0.0.1:6379"):
        assert forbidden not in source, forbidden


def test_runtime_integration_keeps_its_temp_root_off_the_shared_host_tmp() -> None:
    step = load(MIDDLEWARE_CI)["jobs"]["runtime-integration"]["steps"][-1]
    assert step["env"]["TMPDIR"] == "${{ runner.temp }}/middleware-runtime-integration"
    assert 'mkdir -p "$TMPDIR"' in step["run"]
    assert step["run"].rstrip().endswith("bash scripts/integration_ci.sh")
