"""Tests for scripts/validate_lane_file_scope.py.

Standard library only (unittest); also runs under pytest. Locates the validator next to
this file, or at scripts/validate_lane_file_scope.py when installed in the repository.
"""

from __future__ import annotations

import copy
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
_CANDIDATES = [HERE / "validate_lane_file_scope.py", HERE.parent / "scripts" / "validate_lane_file_scope.py"]
_PATH = next(p for p in _CANDIDATES if p.is_file())
_spec = importlib.util.spec_from_file_location("validate_lane_file_scope", _PATH)
v = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
sys.modules[_spec.name] = v
_spec.loader.exec_module(v)

_POLICY_CANDIDATES = [HERE / "sample" / "governance" / "file-ownership.v1.json", HERE.parent / "governance" / "file-ownership.v1.json"]
SAMPLE = next((p for p in _POLICY_CANDIDATES if p.is_file()), _POLICY_CANDIDATES[-1])
SHA_A = "a" * 40

CODEOWNERS = """\
* @appolon1908 @kazan555
/app/ @appolon1908
/.codestra/validate-release-intent.py @kazan555
"""


def lane(lane_id, branch, owns, status="ACTIVE", review=("@appolon1908",), worktree=None):
    return {
        "id": lane_id,
        "branch": branch,
        "worktree": worktree or f"/wt/{lane_id.split('::')[-1]}",
        "status": status,
        "file_scope": {"owns": list(owns)},
        "review_authority": list(review),
    }


def base_policy():
    return {
        "schema": v.SCHEMA_ID,
        "scope_version": 1,
        "base": "main",
        "max_concurrent_writers": 3,
        "coverage": "complete",
        "lanes": [
            lane("fix-lane::identity", "fix/identity", ["app/core/jwt_auth.py", "app/api/v1/sales.py"]),
            lane("fix-lane::adapters", "fix/adapters", ["app/adapters/n8n/", "app/telnexa_provider_adapter.py"]),
            lane(
                "fix-lane::supply",
                "fix/supply",
                ["Dockerfile", "requirements*", ".codestra/validate-release-intent.py"],
                review=("@appolon1908", "@kazan555"),
            ),
        ],
        "shared_files": [],
        "unassigned": ["docs/", "README.md"],
    }


TRACKED = [
    "app/core/jwt_auth.py",
    "app/api/v1/sales.py",
    "app/adapters/n8n/transport.py",
    "app/adapters/n8n/__init__.py",
    "app/telnexa_provider_adapter.py",
    "Dockerfile",
    "requirements.in",
    "requirements-runtime.txt",
    ".codestra/validate-release-intent.py",
    "docs/x.md",
    "README.md",
]


def schema(raw):
    rep = v.Report()
    pol = v.check_schema(raw, rep)
    return pol, rep


class GlobTests(unittest.TestCase):
    def test_valid_and_invalid_globs(self):
        for good in ["app/core/jwt_auth.py", "requirements*", "app/adapters/n8n/", "tests/**/test_*.py", "a/[ab].py"]:
            self.assertIsNone(v.glob_error(good), good)
        for bad in ["", "/app/x.py", "./x", "app/{a,b}.py", "app/../x", "app/a**b", "!x", "a\\b", "a/[b.py", " x"]:
            self.assertIsNotNone(v.glob_error(bad), bad)

    def test_glob_semantics(self):
        rx = v.compile_glob("app/adapters/n8n/")
        self.assertTrue(rx.match("app/adapters/n8n/transport.py"))
        self.assertFalse(rx.match("app/adapters/n8n_other.py"))
        self.assertTrue(v.compile_glob("app/adapters/n8n").match("app/adapters/n8n/sub/x.py"))
        self.assertFalse(v.compile_glob("app/*.py").match("app/core/x.py"))
        self.assertTrue(v.compile_glob("tests/**/test_*.py").match("tests/test_a.py"))
        self.assertTrue(v.compile_glob("tests/**/test_*.py").match("tests/integration/test_a.py"))
        self.assertTrue(v.compile_glob("requirements*").match("requirements-test.lock"))
        self.assertFalse(v.compile_glob("requirements*").match("websocket_gateway/requirements.lock"))

    def test_specificity_literal_beats_glob(self):
        self.assertGreater(v.specificity("app/api/v1/sales.py"), v.specificity("app/api/**"))
        self.assertGreater(v.specificity("app/api/v1/**"), v.specificity("app/**"))


class SchemaTests(unittest.TestCase):  # check (a)
    def test_base_policy_is_valid(self):
        pol, rep = schema(base_policy())
        self.assertIsNotNone(pol)
        self.assertEqual(rep.errors, [])

    def test_missing_and_unknown_keys(self):
        raw = base_policy()
        del raw["unassigned"]
        raw["extra"] = 1
        _, rep = schema(raw)
        self.assertTrue(any("missing keys ['unassigned']" in e for e in rep.errors))
        self.assertTrue(any("unknown keys ['extra']" in e for e in rep.errors))

    def test_invalid_glob_in_owns(self):
        raw = base_policy()
        raw["lanes"][0]["file_scope"]["owns"].append("app/{a,b}.py")
        _, rep = schema(raw)
        self.assertTrue(rep.failed("a"))

    def test_catch_all_owns_rejected(self):
        raw = base_policy()
        raw["lanes"][0]["file_scope"]["owns"] = ["**"]
        _, rep = schema(raw)
        self.assertTrue(any("catch-all" in e for e in rep.errors))

    def test_branch_must_be_fix_and_worktrees_distinct(self):
        raw = base_policy()
        raw["lanes"][0]["branch"] = "main"
        raw["lanes"][2]["worktree"] = raw["lanes"][1]["worktree"]
        _, rep = schema(raw)
        self.assertTrue(any("fix/* branch" in e for e in rep.errors))
        self.assertTrue(any("duplicate worktree" in e for e in rep.errors))

    def test_active_lanes_capped(self):
        raw = base_policy()
        raw["max_concurrent_writers"] = 2
        _, rep = schema(raw)
        self.assertTrue(any("exceed max_concurrent_writers=2" in e for e in rep.errors))

    def test_catch_all_unassigned_only_in_partial_mode(self):
        raw = base_policy()
        raw["unassigned"] = ["**"]
        _, rep = schema(raw)
        self.assertTrue(rep.failed("a"))
        raw["coverage"] = "partial"
        _, rep = schema(raw)
        self.assertFalse(rep.failed("a"))

    def test_handoff_requires_recorded_parent_sha(self):
        raw = base_policy()
        raw["shared_files"] = [
            {
                "path": ".codestra/validate-release-intent.py",
                "owner": "fix-lane::supply",
                "handoff_order": ["fix-lane::supply", "fix-lane::identity"],
                "holder": "fix-lane::identity",
                "unit": "file",
                "requires_parent_sha": True,
                "handoff_log": [],
            }
        ]
        _, rep = schema(raw)
        self.assertTrue(any("lacks recorded pushed SHA" in e for e in rep.errors))
        raw["shared_files"][0]["handoff_log"] = [{"lane": "fix-lane::supply", "pushed_sha": "abc", "recorded_at": "2026-10-30T00:00:00Z"}]
        _, rep = schema(raw)
        self.assertTrue(any("40-hex" in e for e in rep.errors))
        raw["shared_files"][0]["handoff_log"][0]["pushed_sha"] = SHA_A
        _, rep = schema(raw)
        self.assertEqual(rep.errors, [])

    def test_handoff_order_starts_with_owner(self):
        raw = base_policy()
        raw["shared_files"] = [
            {
                "path": "Dockerfile",
                "owner": "fix-lane::supply",
                "handoff_order": ["fix-lane::identity", "fix-lane::supply"],
                "holder": None,
                "unit": "file",
                "requires_parent_sha": True,
                "handoff_log": [],
            }
        ]
        _, rep = schema(raw)
        self.assertTrue(any("must start with the owner" in e for e in rep.errors))


class DisjointTests(unittest.TestCase):  # check (b)
    def test_disjoint_passes(self):
        pol, _ = schema(base_policy())
        rep = v.Report()
        v.check_disjoint(pol, TRACKED, rep, strict=True)
        self.assertEqual(rep.errors, [])

    def test_overlap_must_fail(self):
        raw = base_policy()
        raw["lanes"][1]["file_scope"]["owns"].append("app/core/jwt_auth.py")  # also owned by identity
        pol, _ = schema(raw)
        rep = v.Report()
        v.check_disjoint(pol, TRACKED, rep)
        self.assertTrue(rep.failed("b"))
        self.assertTrue(any("app/core/jwt_auth.py" in e for e in rep.errors))

    def test_equal_specificity_glob_overlap_fails_even_for_untracked_paths(self):
        raw = base_policy()
        raw["lanes"][0]["file_scope"]["owns"].append("app/core/*.py")
        raw["lanes"][1]["file_scope"]["owns"].append("app/core/*.py")
        pol, _ = schema(raw)
        rep = v.Report()
        v.check_disjoint(pol, [], rep)
        self.assertTrue(any("declared by both" in e for e in rep.errors))

    def test_specificity_resolution_warns_but_strict_fails(self):
        raw = base_policy()
        raw["lanes"][1]["file_scope"]["owns"].append("app/api/")  # broader than identity's sales.py
        pol, _ = schema(raw)
        rep = v.Report()
        v.check_disjoint(pol, TRACKED, rep)
        self.assertFalse(rep.failed("b"))
        self.assertTrue(any("resolved to 'fix-lane::identity'" in w for w in rep.warnings))
        rep = v.Report()
        v.check_disjoint(pol, TRACKED, rep, strict=True)
        self.assertTrue(rep.failed("b"))

    def test_shared_owner_must_own_path(self):
        raw = base_policy()
        raw["shared_files"] = [
            {
                "path": "Dockerfile",
                "owner": "fix-lane::identity",
                "handoff_order": ["fix-lane::identity"],
                "holder": "fix-lane::identity",
                "unit": "file",
                "requires_parent_sha": False,
                "handoff_log": [],
            }
        ]
        pol, _ = schema(raw)
        rep = v.Report()
        v.check_disjoint(pol, TRACKED, rep)
        self.assertTrue(any("owns resolves to 'fix-lane::supply'" in e for e in rep.errors))


class CoverageTests(unittest.TestCase):  # check (c)
    def test_every_path_mapped(self):
        pol, _ = schema(base_policy())
        rep = v.Report()
        counts = v.check_coverage(pol, TRACKED, rep)
        self.assertEqual(rep.errors, [])
        self.assertEqual(counts["unmapped"], 0)
        self.assertEqual(counts["unassigned"], 2)

    def test_unmapped_path_fails(self):
        pol, _ = schema(base_policy())
        rep = v.Report()
        v.check_coverage(pol, TRACKED + ["app/new_module.py"], rep)
        self.assertTrue(any("app/new_module.py: maps to no lane" in e for e in rep.errors))

    def test_partial_mode_catch_all(self):
        raw = base_policy()
        raw["coverage"] = "partial"
        raw["unassigned"] = ["**"]
        pol, _ = schema(raw)
        rep = v.Report()
        v.check_coverage(pol, TRACKED + ["app/new_module.py"], rep)
        self.assertEqual(rep.errors, [])


class DiffTests(unittest.TestCase):  # check (d)
    def setUp(self):
        raw = base_policy()
        raw["shared_files"] = [
            {
                "path": ".codestra/validate-release-intent.py",
                "owner": "fix-lane::supply",
                "handoff_order": ["fix-lane::supply", "fix-lane::identity"],
                "holder": "fix-lane::supply",
                "unit": "file",
                "requires_parent_sha": True,
                "handoff_log": [],
            }
        ]
        self.raw = raw
        self.pol, rep = schema(raw)
        self.assertEqual(rep.errors, [])

    def test_in_scope_diff_passes(self):
        rep = v.Report()
        v.check_diff(self.pol, self.pol.lane("fix-lane::adapters"), ["app/adapters/n8n/transport.py", "app/adapters/n8n/new.py"], rep)
        self.assertEqual(rep.errors, [])

    def test_out_of_scope_diff_must_fail(self):
        rep = v.Report()
        v.check_diff(self.pol, self.pol.lane("fix-lane::adapters"), ["app/adapters/n8n/transport.py", "app/core/jwt_auth.py", "docs/x.md"], rep)
        self.assertTrue(any("app/core/jwt_auth.py: outside lane" in e and "owned by 'fix-lane::identity'" in e for e in rep.errors))
        self.assertTrue(any("docs/x.md: outside lane" in e for e in rep.errors))

    def test_shared_file_only_while_held(self):
        rep = v.Report()
        v.check_diff(self.pol, self.pol.lane("fix-lane::identity"), [".codestra/validate-release-intent.py"], rep)
        self.assertTrue(any("currently held by 'fix-lane::supply'" in e for e in rep.errors))
        raw = copy.deepcopy(self.raw)
        raw["shared_files"][0]["holder"] = "fix-lane::identity"
        raw["shared_files"][0]["handoff_log"] = [{"lane": "fix-lane::supply", "pushed_sha": SHA_A, "recorded_at": "2026-10-30T00:00:00Z"}]
        pol, rep0 = schema(raw)
        self.assertEqual(rep0.errors, [])
        rep = v.Report()
        v.check_diff(pol, pol.lane("fix-lane::identity"), [".codestra/validate-release-intent.py"], rep)
        self.assertEqual(rep.errors, [])
        rep = v.Report()  # once handed off, the owner may no longer write it either
        v.check_diff(pol, pol.lane("fix-lane::supply"), [".codestra/validate-release-intent.py"], rep)
        self.assertTrue(rep.failed("d"))

    def test_non_active_lane_cannot_write(self):
        raw = copy.deepcopy(self.raw)
        raw["lanes"][0]["status"] = "PLANNED"
        pol, _ = schema(raw)
        rep = v.Report()
        v.check_diff(pol, pol.lane("fix-lane::identity"), ["app/core/jwt_auth.py"], rep)
        self.assertTrue(any("only ACTIVE lanes may write" in e for e in rep.errors))


class ReviewAuthorityTests(unittest.TestCase):  # check (e)
    def test_codeowners_semantics(self):
        rules = v.parse_codeowners(CODEOWNERS + "*.md @docs-owner\n")
        self.assertEqual(v.codeowners_for("app/x.py", rules), ("@appolon1908",))
        self.assertEqual(v.codeowners_for("Dockerfile", rules), ("@appolon1908", "@kazan555"))
        self.assertEqual(v.codeowners_for(".codestra/validate-release-intent.py", rules), ("@kazan555",))
        self.assertEqual(v.codeowners_for("app/deep/README.md", rules), ("@docs-owner",))

    def test_consistent_review_authority_passes(self):
        pol, _ = schema(base_policy())
        rep = v.Report()
        derived = v.check_review_authority(pol, TRACKED, v.parse_codeowners(CODEOWNERS), rep)
        self.assertEqual(rep.errors, [], derived)

    def test_missing_kazan_on_release_authority_path_fails(self):
        raw = base_policy()
        raw["lanes"][2]["review_authority"] = ["@appolon1908"]
        pol, _ = schema(raw)
        rep = v.Report()
        v.check_review_authority(pol, TRACKED, v.parse_codeowners(CODEOWNERS), rep)
        self.assertTrue(any("fix-lane::supply" in e and "missing ['@kazan555']" in e for e in rep.errors))

    def test_extra_reviewer_fails(self):
        raw = base_policy()
        raw["lanes"][0]["review_authority"] = ["@appolon1908", "@kazan555"]
        pol, _ = schema(raw)
        rep = v.Report()
        v.check_review_authority(pol, TRACKED, v.parse_codeowners(CODEOWNERS), rep)
        self.assertTrue(any("extra ['@kazan555']" in e for e in rep.errors))

    def test_shared_participation_adds_shared_file_owners(self):
        raw = base_policy()
        raw["shared_files"] = [
            {
                "path": ".codestra/validate-release-intent.py",
                "owner": "fix-lane::supply",
                "handoff_order": ["fix-lane::supply", "fix-lane::identity"],
                "holder": "fix-lane::supply",
                "unit": "file",
                "requires_parent_sha": True,
                "handoff_log": [],
            }
        ]
        pol, _ = schema(raw)
        rep = v.Report()
        v.check_review_authority(pol, TRACKED, v.parse_codeowners(CODEOWNERS), rep)
        self.assertTrue(any("fix-lane::identity" in e and "missing ['@kazan555']" in e for e in rep.errors))


class SamplePolicyTests(unittest.TestCase):
    @unittest.skipUnless(SAMPLE.is_file(), "sample policy not present")
    def test_sample_schema_and_strict_disjoint(self):
        pol, rep = schema(json.loads(SAMPLE.read_text()))
        self.assertEqual(rep.errors, [])
        rep = v.Report()
        v.check_disjoint(pol, [], rep, strict=True)
        self.assertEqual(rep.errors, [])
        self.assertLessEqual(sum(l.status == "ACTIVE" for l in pol.lanes), pol.max_concurrent_writers)


class CliEndToEndTests(unittest.TestCase):
    def _git(self, root, *args):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.root = root
        self._git(root, "init", "-q", "-b", "main")
        self._git(root, "config", "user.email", "t@example.invalid")
        self._git(root, "config", "user.name", "t")
        for path in TRACKED:
            p = root / path
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("x\n")
        (root / ".github").mkdir()
        (root / ".github/CODEOWNERS").write_text(CODEOWNERS)
        raw = base_policy()
        raw["unassigned"] += [".github/", "governance/"]
        (root / "governance").mkdir()
        (root / v.DEFAULT_POLICY).write_text(json.dumps(raw, indent=2))
        self._git(root, "add", "-A")
        self._git(root, "commit", "-qm", "base")
        self._git(root, "checkout", "-qb", "fix/adapters")

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, *extra):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = v.run(["--root", str(self.root), "--strict", "--diff", "--base", "main", *extra])
        return code, buf.getvalue()

    def test_in_scope_branch_passes(self):
        (self.root / "app/adapters/n8n/transport.py").write_text("y\n")
        self._git(self.root, "commit", "-qam", "fix(n8n): change")
        code, out = self._run("--require-current-base")
        self.assertEqual(code, 0, out)
        self.assertIn("LANE_SCOPE_DIFF=PASS", out)

    def test_out_of_scope_branch_fails(self):
        (self.root / "app/adapters/n8n/transport.py").write_text("y\n")
        (self.root / "app/core/jwt_auth.py").write_text("y\n")
        self._git(self.root, "commit", "-qam", "fix(n8n): change plus stray edit")
        code, out = self._run()
        self.assertEqual(code, 1, out)
        self.assertIn("LANE_SCOPE_DIFF=FAIL", out)
        self.assertIn("app/core/jwt_auth.py: outside lane 'fix-lane::adapters'", out)

    def test_uncommitted_out_of_scope_detected_with_include_worktree(self):
        (self.root / "Dockerfile").write_text("y\n")
        code, _ = self._run()
        self.assertEqual(code, 0)
        code, out = self._run("--include-worktree")
        self.assertEqual(code, 1, out)
        self.assertIn("Dockerfile: outside lane", out)

    def test_stale_base_detected(self):
        self._git(self.root, "checkout", "-q", "main")
        (self.root / "README.md").write_text("main moved\n")
        self._git(self.root, "commit", "-qam", "docs: move main")
        self._git(self.root, "checkout", "-q", "fix/adapters")
        code, out = self._run("--require-current-base")
        self.assertEqual(code, 1, out)
        self.assertIn("not based on current main", out)


if __name__ == "__main__":
    unittest.main()
