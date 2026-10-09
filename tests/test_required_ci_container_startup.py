"""Guard the required job against accepting PostgreSQL's temporary init server."""

import shlex
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_postgres_waits_for_final_tcp_server_with_bounded_startup():
    workflow = yaml.safe_load((ROOT / ".github/workflows/required-ci.yml").read_text())
    job = workflow["jobs"]["test"]
    postgres = job["services"]["postgres"]
    arguments = shlex.split(postgres["options"])
    options = dict(zip(arguments[::2], arguments[1::2], strict=True))
    probe = shlex.split(options["--health-cmd"])
    assert probe[0] == "pg_isready"
    assert probe[probe.index("-h") + 1] == "127.0.0.1"
    assert probe[probe.index("-U") + 1] == postgres["env"]["POSTGRES_USER"]
    assert probe[probe.index("-d") + 1] == postgres["env"]["POSTGRES_DB"]
    # Startup failures do not exhaust the normal retry budget during initdb.
    grace = options["--health-start-period"]
    assert grace.endswith("s") and 120 <= int(grace[:-1]) <= 180
    assert options["--health-interval"] == "5s"
    assert options["--health-timeout"] == "5s"
    assert options["--health-retries"] == "12"
    assert postgres["ports"] == ["5432/tcp"]
    assert postgres["image"] == (
        "postgres@sha256:742f40ea20b9ff2ff31db5458d127452988a2164df9e17441e191f3b72252193"
    )
    assert job["timeout-minutes"] == 30
    checkout = next(step for step in job["steps"] if step.get("name") == "Check out exact target")
    assert checkout["with"]["ref"] == "${{ env.TARGET_SHA }}"
    assert workflow["jobs"]["publish-final-status"]["needs"] == "test"
