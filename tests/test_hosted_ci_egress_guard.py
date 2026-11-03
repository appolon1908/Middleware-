"""Exercise the real guard/installer without touching the host firewall."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "scripts/codestra-ci-egress-guard"
INSTALLER = ROOT / "scripts/install_hosted_ci_egress_guard.sh"
DIGEST = "881f3c1f592dd645659cf46121625f50464644ef0685c4f752a1e46831e56b5a"
TARGETS = ("65.109.65.169/32", "65.21.67.207/32", "37.27.128.39/32", "10.40.0.0/24")


def run_guard(tmp_path: Path, command: str) -> subprocess.CompletedProcess[str]:
    # Bash can intercept this absolute command name. The checked-in guard bytes
    # are executed unchanged; only the privileged iptables boundary is replaced.
    assert GUARD.is_file(), "The exact reviewed guard must be available on a fresh runner"
    recorder = tmp_path / "iptables.py"
    recorder.write_text('''import json, os, sys
from pathlib import Path
path = Path(os.environ["RULES"])
rules = json.loads(path.read_text()) if path.exists() else []
args = sys.argv[1:]
if args[0] == "-C":
    sys.exit(0 if args[1:] in rules else 1)
assert args[0] == "-I" and args[2] == "1", args
rules.append([args[1], *args[3:]])
path.write_text(json.dumps(rules))
''')
    environment = {"PATH": "/usr/bin:/bin", "PYTHON": sys.executable,
                   "RECORDER": str(recorder), "RULES": str(tmp_path / "rules.json")}
    return subprocess.run(
        ["bash", "--noprofile", "--norc", "-c",
         'function /usr/sbin/iptables() { "$PYTHON" "$RECORDER" "$@"; }; source "$1" "$2"',
         "guard-test", str(GUARD), command],
        env=environment, capture_output=True, text=True, check=False,
    )


def test_guard_preserves_reviewed_bytes_and_installs_only_eight_fixed_reject_rules(tmp_path: Path) -> None:
    result = run_guard(tmp_path, "apply")
    assert result.returncode == 0, result.stderr
    assert hashlib.sha256(GUARD.read_bytes()).hexdigest() == DIGEST
    expected = [[chain, "-d", target, "-j", "REJECT"]
                for target in TARGETS for chain in ("OUTPUT", "FORWARD")]
    assert json.loads((tmp_path / "rules.json").read_text()) == expected
    assert run_guard(tmp_path, "apply").returncode == 0
    assert json.loads((tmp_path / "rules.json").read_text()) == expected
    assert run_guard(tmp_path, "check").returncode == 0


@pytest.mark.parametrize("missing_rule", range(8))
def test_guard_rejects_each_missing_rule_without_repair_during_check(tmp_path: Path, missing_rule: int) -> None:
    assert run_guard(tmp_path, "apply").returncode == 0
    path = tmp_path / "rules.json"
    rules = json.loads(path.read_text())
    rules.pop(missing_rule)
    path.write_text(json.dumps(rules))
    assert run_guard(tmp_path, "check").returncode != 0
    assert json.loads(path.read_text()) == rules


def run_installer(tmp_path: Path, overrides: dict[str, str] | None = None, tamper: bool = False):
    assert INSTALLER.is_file(), "Installation must be available and explicitly refuse persistent runners"
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / INSTALLER.name).write_bytes(INSTALLER.read_bytes())
    (scripts / GUARD.name).write_bytes(GUARD.read_bytes() + (b"# tampered\n" if tamper else b""))
    # Privileged installation/checks are redirected into this test's directory.
    # No sudo or firewall command is ever executed on the developer host.
    sudo = tmp_path / "sudo"
    sudo.write_text(f"#!{sys.executable}\n" + '''import json, os, shutil, subprocess, sys
from pathlib import Path
args = sys.argv[1:]
assert args.pop(0) == "-n"
root = Path(os.environ["INSTALL_TEST_ROOT"])
with (root / "sudo-calls.jsonl").open("a") as stream:
    stream.write(json.dumps(args) + "\\n")
target = "/usr/local/sbin/codestra-ci-egress-guard"
assert args[-1] == target, args
if args[0] == "install":
    assert args[1:7] == ["-o", "root", "-g", "root", "-m", "0755"], args
    shutil.copyfile(args[-2], root / "installed")
    (root / "installed").chmod(0o755)
elif args[0] == "stat":
    assert args[1:-1] == ["-c", "%u:%g:%a", "--"], args
    print(os.environ.get("INSTALLED_OWNER", "0:0:755"))
elif args[0] == "sha256sum":
    if os.environ.get("CORRUPT_INSTALL") == "true":
        (root / "installed").write_text("changed after installation")
    subprocess.run(["/usr/bin/sha256sum", str(root / "installed")], check=True)
else:
    raise AssertionError(args)
''')
    sudo.chmod(0o755)
    environment = {"PATH": f"{tmp_path}:/usr/bin:/bin", "INSTALL_TEST_ROOT": str(tmp_path),
                   "GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "github-hosted",
                   "RUNNER_OS": "Linux", "RUNNER_ARCH": "X64", "ImageOS": "ubuntu24",
                   "ImageVersion": "20261102.1.0", **(overrides or {})}
    return subprocess.run(["bash", str(scripts / INSTALLER.name)], env=environment,
                          capture_output=True, text=True, check=False)


def test_installer_checks_hosted_context_hash_and_root_owned_mode(tmp_path: Path) -> None:
    result = run_installer(tmp_path)
    assert result.returncode == 0, result.stderr
    assert hashlib.sha256((tmp_path / "installed").read_bytes()).hexdigest() == DIGEST
    assert (tmp_path / "installed").stat().st_mode & 0o777 == 0o755
    calls = [json.loads(line) for line in (tmp_path / "sudo-calls.jsonl").read_text().splitlines()]
    assert [call[0] for call in calls] == ["install", "stat", "sha256sum"]


@pytest.mark.parametrize(("variable", "value"), [
    ("GITHUB_ACTIONS", ""), ("GITHUB_ACTIONS", "false"),
    ("RUNNER_ENVIRONMENT", "self-hosted"), ("RUNNER_ENVIRONMENT", ""),
    ("RUNNER_OS", "Windows"), ("RUNNER_ARCH", "ARM64"),
    ("ImageOS", ""), ("ImageOS", "ubuntu22"), ("ImageVersion", ""),
])
def test_installer_refuses_nonhosted_context_before_privileged_effects(tmp_path: Path, variable: str, value: str) -> None:
    result = run_installer(tmp_path, {variable: value})
    assert result.returncode != 0
    assert not (tmp_path / "sudo-calls.jsonl").exists()


def test_installer_refuses_changed_guard_before_privileged_effects(tmp_path: Path) -> None:
    result = run_installer(tmp_path, tamper=True)
    assert result.returncode != 0
    assert not (tmp_path / "sudo-calls.jsonl").exists()


@pytest.mark.parametrize("override", [{"INSTALLED_OWNER": "1000:1000:755"}, {"CORRUPT_INSTALL": "true"}])
def test_installer_rejects_incorrect_privileged_installation(tmp_path: Path, override: dict[str, str]) -> None:
    assert run_installer(tmp_path, override).returncode != 0


def test_hosted_workflow_enforces_guard_before_product_execution() -> None:
    jobs = yaml.safe_load((ROOT / ".github/workflows/required-ci.yml").read_text())["jobs"]
    assert all(job["runs-on"] == "ubuntu-24.04" for job in jobs.values())
    steps = jobs["test"]["steps"]
    ids = [step.get("id") for step in steps]
    for producer in ("egress_install", "egress_apply", "egress_check"):
        assert ids.index("evidence_init") < ids.index(producer) < ids.index("dependencies")
        step = next(step for step in steps if step.get("id") == producer)
        assert not step.get("continue-on-error", False)
        assert not step.get("if")
