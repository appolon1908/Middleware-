"""Use a stacked PR's direct parent for promotion while keeping trunk rules."""
from datetime import date
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/codestra-hierarchy-governance.yml"
POLICY = ROOT / "governance/promotion-policy.json"
HEAD = "convergence/middleware-saas-certified-20261005"
PARENT = "fix/source-hygiene-post430-20261005"


def context(head=HEAD, parent=PARENT):
    # GitHub's documented native stack context: Actions targets the trunk;
    # the webhook pull_request.base still names the immediate parent branch.
    return {"github": {
        "head_ref": head, "base_ref": "main",
        "event": {"pull_request": {
            "head": {"ref": head, "sha": "a" * 40}, "base": {"ref": parent},
            "stack": {"base": {"ref": "main"}},
        }},
    }}


def expression(value, data):
    match = re.fullmatch(r"\$\{\{\s*(github(?:\.[a-z_]+)+)\s*\}\}", value)
    if match is None:
        raise AssertionError(f"not a simple GitHub property expression: {value}")
    result = data
    for key in match.group(1).split("."):
        result = result[key]
    return result


def guard_step(text):
    return text.split("      - name: Enforce promotion path\n", 1)[1].split("\n\n", 1)[0]


def controller_step(text):
    return text.split(
        "      - name: Verify exact SHA in Codestra Development Department control plane\n", 1
    )[1].split("\n\n", 1)[0]


def configured_guard(data):
    step = guard_step(WORKFLOW.read_text())
    pairs = re.findall(r"^\s+([A-Z_]+): (\$\{\{[^\n]+\}\})$", step, re.MULTILINE)
    environment = dict(os.environ, GITHUB_HEAD_REF=data["github"]["head_ref"],
                       GITHUB_BASE_REF=data["github"]["base_ref"])
    for name, value in pairs:
        # Reserved GitHub defaults stay authoritative; custom variables and
        # the guard's existing CLI arguments carry direct-parent metadata.
        if not name.startswith(("GITHUB_", "RUNNER_")):
            environment[name] = expression(value, data)
    run = re.search(r"^\s+run: (.+)$", step, re.MULTILINE)
    if run is None:
        raise AssertionError("promotion guard command is missing")
    command = shlex.split(run.group(1))
    command = [environment[item[1:]] if re.fullmatch(r"\$[A-Z_]+", item) else item
               for item in command]
    command[0] = sys.executable
    return subprocess.run(command, cwd=ROOT, env=environment,
                          capture_output=True, text=True, check=False)


class HierarchyStackContextTests(unittest.TestCase):
    def test_all_promotion_consumers_resolve_the_immediate_parent(self):
        text = WORKFLOW.read_text()
        guard_base = re.search(r"^\s+(?:GITHUB|PROMOTION)_BASE_REF: (.+)$",
                               guard_step(text), re.MULTILINE)
        controller = controller_step(text)
        controller_base = re.search(r"^\s+BASE_REF: (.+)$", controller, re.MULTILINE)
        if controller_base is None:
            controller_base = re.search(r"--base-ref '(\$\{\{[^\n]+\}\})'", controller)
        gate_base = re.search(r"^\s+BASE_REF: (.+)$", text.split("  promotion-gate:", 1)[1],
                              re.MULTILINE)
        for name, match in (("guard", guard_base), ("control plane", controller_base),
                            ("gate", gate_base)):
            with self.subTest(consumer=name):
                self.assertIsNotNone(match)
                self.assertEqual(expression(match.group(1), context()), PARENT)

    def test_current_stacked_migration_obeys_unchanged_policy(self):
        policy = json.loads(POLICY.read_text())
        row = next((row for row in policy.get("migration_exceptions", [])
                    if row.get("head") == HEAD and row.get("base") == PARENT), None)
        if row is None:
            self.skipTest("historical migration is no longer configured")
        active = date.today() <= date.fromisoformat(row["expires"])
        result = configured_guard(context())
        self.assertEqual(result.returncode, 0 if active else 1, result.stdout + result.stderr)
        self.assertIn("PASS migration" if active else "expired_migration", result.stdout)
        self.assertIn(f"base={PARENT}", result.stdout)

    def test_stacked_accepted_hierarchy_uses_its_direct_section(self):
        result = configured_guard(context("subsection/unit--change", "section/unit"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("base=section/unit", result.stdout)

    def test_direct_convergence_to_main_still_fails_closed(self):
        result = configured_guard(context(parent="main"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f"BLOCK head={HEAD} base=main", result.stdout)

    def test_controller_shell_preserves_valid_quoted_branch_arguments(self):
        data = context("subsection/unit'--change", "section/unit'")
        for branch in (data["github"]["head_ref"], data["github"]["event"]["pull_request"]["base"]["ref"]):
            self.assertEqual(subprocess.run(
                ["git", "check-ref-format", "--branch", branch], capture_output=True
            ).returncode, 0)
        accepted = configured_guard(data)
        self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)
        step = controller_step(WORKFLOW.read_text())
        pairs = re.findall(r"^\s+([A-Z_]+): (\$\{\{[^\n]+\}\})$", step, re.MULTILINE)
        environment = dict(os.environ)
        for name, value in pairs:
            self.assertFalse(name.startswith(("GITHUB_", "RUNNER_")))
            environment[name] = expression(value, data)
        script = textwrap.dedent(step.split("        run: |\n", 1)[1])
        script = re.sub(r"\$\{\{[^\n]+?\}\}",
                        lambda match: expression(match.group(0), data), script)
        checker = "/srv/codestra/dev-platform/control-plane/bin/codestra-github-check"
        self.assertEqual(script.count(checker), 1)
        with tempfile.TemporaryDirectory() as directory:
            recorder = Path(directory) / "record-arguments"
            recorder.write_text(f"#!{sys.executable}\nimport json, sys\nprint(json.dumps(sys.argv[1:]))\n")
            recorder.chmod(0o755)
            # Execute the configured shell boundary with a local argv recorder;
            # the real control-plane checker is never invoked by this test.
            script = script.replace(checker, shlex.quote(str(recorder)))
            result = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", script],
                                    env=environment, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        arguments = json.loads(result.stdout)
        self.assertEqual(arguments[arguments.index("--head-ref") + 1], data["github"]["head_ref"])
        self.assertEqual(arguments[arguments.index("--base-ref") + 1], "section/unit'")
        self.assertEqual(arguments[arguments.index("--head-sha") + 1], "a" * 40)

    def test_main_remains_a_workflow_trigger(self):
        triggers = WORKFLOW.read_text().split("permissions:", 1)[0]
        self.assertIn("      - main\n", triggers)


if __name__ == "__main__":
    unittest.main(verbosity=2)
