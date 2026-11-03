"""Native source CI runs on every PR and publishes its actual test result."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github/workflows"


def workflow(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", ["middleware-ci.yml", "required-ci.yml"])
def test_source_suites_run_for_every_pull_request_target(name: str) -> None:
    document = workflow(name)
    events = document.get("on", document.get(True))
    assert isinstance(events, dict)
    assert "pull_request" in events
    assert events["pull_request"] in (None, {})


def test_source_jobs_do_not_depend_on_installed_promotion_controller() -> None:
    for path in sorted(WORKFLOWS.glob("*.y*ml")):
        document = workflow(path.name)
        events = document.get("on", document.get(True))
        if not isinstance(events, dict) or "pull_request" not in events:
            continue
        for job_id, job in document["jobs"].items():
            commands = "\n".join(step.get("run", "") for step in job.get("steps", []))
            for dependency in ("codestra-github-check", "governance/promotion_guard.py"):
                assert dependency not in commands, f"{path.name}:{job_id}: {dependency}"


@pytest.mark.parametrize(
    ("result", "expected_state"),
    [("success", "success"), ("failure", "failure"), ("cancelled", "error"),
     ("skipped", "error"), ("unknown", "error"), ("", "error")],
)
def test_exact_sha_status_publisher_uses_actual_product_test_result(
    tmp_path: Path, result: str, expected_state: str,
) -> None:
    document = workflow("required-ci.yml")
    job = document["jobs"]["publish-final-status"]
    assert job["needs"] == "test"
    assert job["if"] == "${{ always() }}"
    step = job["steps"][0]
    assert step["env"]["TEST_RESULT"] == "${{ needs.test.result }}"
    assert "github.event.pull_request.head.sha" in document["env"]["TARGET_SHA"]
    assert document["env"]["STATUS_CONTEXT"] == "codestra/required-ci"

    # Run the real workflow shell with a local HTTP recorder: no request leaves
    # the test, and no inherited credential or provider configuration is used.
    recorder = tmp_path / "curl"
    recorder.write_text(
        f"#!{sys.executable}\nimport json, pathlib, sys\n"
        "pathlib.Path('request.json').write_text(json.dumps(sys.argv[1:]))\n"
        "print('{}')\n",
        encoding="utf-8",
    )
    recorder.chmod(0o755)
    target_sha = "a" * 40
    run_url = "https://github.com/example/middleware/actions/runs/123"
    environment = {
        "PATH": os.pathsep.join((str(tmp_path), str(Path(sys.executable).parent), "/usr/bin", "/bin")),
        "GH_TOKEN": "synthetic-test-token",
        "TEST_RESULT": result,
        "GITHUB_RUN_ID": "123",
        "GITHUB_REPOSITORY": "example/middleware",
        "TARGET_SHA": target_sha,
        "STATUS_CONTEXT": document["env"]["STATUS_CONTEXT"],
        "RUN_URL": run_url,
    }
    completed = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", step["run"]],
        cwd=tmp_path, env=environment, capture_output=True, text=True, check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    status = json.loads((tmp_path / "final-status.json").read_text())
    assert status["state"] == expected_state
    assert status["context"] == "codestra/required-ci"
    assert status["target_url"] == run_url
    request = json.loads((tmp_path / "request.json").read_text())
    assert request[request.index("--request") + 1] == "POST"
    assert f"https://api.github.com/repos/example/middleware/statuses/{target_sha}" in request
    assert request[request.index("--data") + 1] == "@final-status.json"
