from __future__ import annotations

import copy
import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "apply_portfolio_main_release_authorities.py"
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location("portfolio_main_release_authorities", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class PortfolioMainReleaseAuthoritiesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = MODULE.load_config()

    def test_committed_authority_is_exact(self) -> None:
        records = MODULE.validate_config(self.config)
        self.assertEqual(
            {record["repository"] for record in records},
            set(MODULE.EXPECTED_REPOSITORIES),
        )

    def test_every_ruleset_is_fail_closed(self) -> None:
        records = MODULE.validate_config(self.config)
        for record in records:
            value = MODULE.normalize_ruleset(MODULE.desired_ruleset(self.config, record))
            self.assertEqual(value["bypass_actors"], [])
            self.assertEqual(
                value["conditions"],
                {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
            )
            rules = value["rules"]
            self.assertTrue(rules["deletion"])
            self.assertTrue(rules["non_fast_forward"])
            self.assertTrue(rules["required_linear_history"])
            self.assertEqual(
                rules["pull_request"],
                {
                    "allowed_merge_methods": ["squash"],
                    "dismiss_stale_reviews_on_push": True,
                    "require_code_owner_review": False,
                    "require_last_push_approval": True,
                    "required_approving_review_count": 1,
                    "required_review_thread_resolution": True,
                },
            )
            self.assertEqual(
                rules["required_status_checks"]["contexts"],
                record["required_status_checks"],
            )
            self.assertTrue(
                rules["required_status_checks"]["strict_required_status_checks_policy"]
            )

    def test_validate_mode_never_requires_a_token_or_mutates_github(self) -> None:
        result = MODULE.execute("validate", "")
        self.assertEqual(result["result"], "PASS")
        self.assertEqual(len(result["repositories"]), 6)
        self.assertTrue(
            all(row["action"] == "policy-validated" for row in result["repositories"])
        )

    def test_zero_approval_policy_is_rejected(self) -> None:
        value = copy.deepcopy(self.config)
        value["required_approvals"] = 0
        with self.assertRaises(MODULE.PolicyError):
            MODULE.validate_config(value)

    def test_repository_or_check_drift_is_rejected(self) -> None:
        value = copy.deepcopy(self.config)
        value["repositories"][0]["required_status_checks"] = ["made-up-check"]
        with self.assertRaises(MODULE.PolicyError):
            MODULE.validate_config(value)

    def test_apply_requires_exact_confirmation_before_api_use(self) -> None:
        with self.assertRaises(MODULE.PolicyError):
            MODULE.execute("apply", "WRONG")


if __name__ == "__main__":
    unittest.main()
