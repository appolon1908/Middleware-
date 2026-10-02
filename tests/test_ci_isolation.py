import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.validate_ci_isolation import FLAGS, validate

ROOT = Path(__file__).resolve().parents[1]


def isolated():
    return {
        "CI_POSTGRES_PORT": "5432",
        "CI_REDIS_PORT": "6379",
        "DATABASE_URL": "postgresql+asyncpg://ci:synthetic@127.0.0.1:5432/middleware_rehearsal",
        "TEST_DATABASE_URL": "postgresql+asyncpg://ci:synthetic@127.0.0.1:5432/middleware_rehearsal",
        "REDIS_URL": "redis://127.0.0.1:6379/15",
        **{name: "false" for name in FLAGS},
    }


def test_validated_isolation_cli():
    result = subprocess.run([sys.executable, str(ROOT / "scripts/validate_ci_isolation.py")], env={**os.environ, **isolated()}, capture_output=True, text=True, check=False)
    assert result.returncode == 0


@pytest.mark.parametrize(
    "name",
    [*FLAGS, "CI_POSTGRES_PORT", "CI_REDIS_PORT", "DATABASE_URL", "TEST_DATABASE_URL", "REDIS_URL"],
)
def test_missing_guard_variable_fails_closed(name):
    environ = isolated()
    environ.pop(name)
    with pytest.raises(ValueError):
        validate(environ)


@pytest.mark.parametrize("target", [
    "postgresql://ci:synthetic@production.invalid:5432/middleware_rehearsal",
    "postgresql://ci:synthetic@127.0.0.1:5432/customer_database",
    "postgresql://ci:synthetic@127.0.0.1:5432/middleware_rehearsal?host=production.invalid",
    "postgresql://ci:synthetic@127.0.0.1:bad/middleware_rehearsal",
])
def test_nonisolated_connection_target_is_rejected(target):
    with pytest.raises(ValueError):
        validate({**isolated(), "DATABASE_URL": target})


def test_workflow_uses_governed_root_owned_egress_guard():
    source = (ROOT / ".github/workflows/required-ci.yml").read_text()
    assert "python scripts/validate_ci_isolation.py" in source
    assert "sudo -n /usr/local/sbin/codestra-ci-egress-guard apply" in source
    assert "sudo -n /usr/local/sbin/codestra-ci-egress-guard check" in source
    assert "sudo -n iptables" not in source
    assert "! rg -n" not in source


def test_readiness_ci_preserves_positive_and_all_dependency_failure_cases():
    source = (ROOT / ".github/workflows/required-ci.yml").read_text()
    assert "redis@sha256:" in source
    assert 'export KEYCLOAK_JWKS_URL="http://127.0.0.1:${jwks_port}/certs.json"' in source
    assert "--bind 127.0.0.1" in source
    assert "audit_case healthy" in source and "            200" in source
    for name in ("wrong-credential", "dns-failure", "tcp-failure", "redis-failure", "keycloak-failure"):
        assert f"audit_case {name}" in source
        assert source.count(f"readiness-{name}.json") == 2


def test_manifest_gate_runs_after_locked_dependencies_and_before_pytest():
    source = (ROOT / "scripts/project_ci.sh").read_text()
    bootstrap = (ROOT / "scripts/run_ci.sh").read_text()
    install = source.index("--require-hashes -r requirements-test.txt")
    validate = source.index("python scripts/validate_codestra_manifest.py")
    tests = source.index("pytest -q tests")
    assert install < validate < tests
    assert "scripts/project_ci.sh" in bootstrap
    assert "python3 scripts/validate_codestra_manifest.py" not in bootstrap


def test_dynamic_runner_ports_remain_isolated():
    environ = isolated()
    environ["CI_POSTGRES_PORT"] = "32783"
    environ["CI_REDIS_PORT"] = "32784"
    environ["DATABASE_URL"] = "postgresql+asyncpg://ci:synthetic@127.0.0.1:32783/middleware_rehearsal"
    environ["TEST_DATABASE_URL"] = "postgresql+asyncpg://ci:synthetic@127.0.0.1:32783/middleware_rehearsal"
    environ["REDIS_URL"] = "redis://127.0.0.1:32784/15"
    validate(environ)


def test_required_ci_rollback_uses_assigned_postgres_port():
    source = (ROOT / ".github/workflows/required-ci.yml").read_text()
    rollback = source.split("- name: Verify rollback by isolated restoration", 1)[1].split("- name: Application startup and disabled defaults", 1)[0]
    assert "PGPORT: ${{ job.services.postgres.ports['5432'] }}" in rollback
    assert rollback.count('-p "${PGPORT}"') == 4
    assert rollback.count("-e PGPASSWORD -e PGPORT") == 4


def test_required_ci_uses_disk_backed_runner_temp_for_heavy_python_steps():
    source = (ROOT / ".github/workflows/required-ci.yml").read_text()
    assert source.count("TMPDIR: ${{ runner.temp }}") >= 2


def test_middleware_ci_uses_dynamic_service_ports_on_hosted_runner():
    source = (ROOT / ".github/workflows/middleware-ci.yml").read_text()
    assert source.count("- 5432/tcp") >= 2
    assert source.count("- 6379/tcp") >= 2
    assert "5432:5432" not in source
    assert "6379:6379" not in source
    assert "job.services.postgres.ports['5432']" in source
    assert "job.services.redis.ports['6379']" in source


HOSTED_RUNNER = "runs-on: ubuntu-24.04"


def test_portable_ci_lanes_use_ephemeral_github_hosted_runners():
    """Portable CI must not depend on the persistent governed runner.

    Host-only gates stay self-hosted below; everything in this list executes
    on GitHub-hosted Ubuntu so repository CI cannot deadlock on runner loss.
    """
    workflows = (
        "middleware-ci.yml",
        "release-component-ci.yml",
        "connector-sdk-ci.yml",
        "connector-runtime-api-ci.yml",
        "integrated-monitoring.yml",
        "marketing-stage5-certification.yml",
        "production-integration-lock.yml",
        "production-reviewer-access.yml",
        "production-route-contract.yml",
        "python-quality-baseline.yml",
        "codeql.yml",
        "staging-intake-observability-contract.yml",
    )
    for name in workflows:
        source = (ROOT / ".github/workflows" / name).read_text(encoding="utf-8")
        assert HOSTED_RUNNER in source, name
        assert "fromJSON('[\"self-hosted\"" not in source, name
        assert "runs-on: [self-hosted" not in source, name



def test_pull_request_target_gates_stay_byte_pinned_on_the_governed_runner():
    # pull_request_target runs base-branch code and is pinned by exact bytes
    # in validate_repository_governance.py; the fork selector does not apply.
    for name in ("production-orchestrator-contract.yml", "trusted-production-orchestrator-gate.yml"):
        source = (ROOT / ".github/workflows" / name).read_text(encoding="utf-8")
        assert "pull_request_target:" in source
        assert "runs-on: [self-hosted, Linux, X64, middleware-ci]" in source


def test_required_ci_stays_on_the_governed_runner_for_its_egress_guard():
    # The root-owned egress guard exists only on the governed host.
    source = (ROOT / ".github/workflows/required-ci.yml").read_text()
    assert "sudo -n /usr/local/sbin/codestra-ci-egress-guard apply" in source
    assert source.count("runs-on: [self-hosted, Linux, X64, middleware-ci]") == 2


def test_required_ci_allocates_job_local_http_probe_ports():
    source = (ROOT / ".github/workflows/required-ci.yml").read_text()
    assert source.count("free_port()") >= 2
    assert 'app_port="$(free_port)"' in source
    assert 'jwks_port="$(free_port)"' in source
    assert 'port="$(free_port)"' in source
    for fixed in (":8095", ":8101", ":8102", ":8103", ":8104", ":8105", ":8106", ":8120"):
        assert fixed not in source
    assert 'kill -0 "${server_pid}"' in source
    assert 'kill -0 "$jwks_pid"' in source
