from __future__ import annotations

import copy
import importlib.util
import json
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "apply_integration_main_release_authorities.py"
CONFIG = ROOT / "config" / "integration-main-release-authorities.v1.json"

spec = importlib.util.spec_from_file_location("integration_authority", SCRIPT)
assert spec and spec.loader
MODULE = importlib.util.module_from_spec(spec)
spec.loader.exec_module(MODULE)


class IntegrationMainReleaseAuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config: dict[str, Any] = json.loads(CONFIG.read_text(encoding="utf-8"))

    def test_exact_fixed_repository_set_validates(self) -> None:
        rows = MODULE.validate_config(self.config)
        self.assertEqual(
            {row["repository"] for row in rows},
            set(MODULE.EXPECTED_REPOSITORIES),
        )
        self.assertEqual(self.config["reviewer"], MODULE.EXPECTED_REVIEWER)

    def test_every_ruleset_is_fail_closed(self) -> None:
        for row in MODULE.validate_config(self.config):
            normalized = MODULE.normalize_ruleset(MODULE.desired_ruleset(row))
            self.assertEqual(normalized["bypass_actors"], [])
            self.assertEqual(normalized["conditions"]["ref_name"]["include"], ["~DEFAULT_BRANCH"])
            pull = normalized["rules"]["pull_request"]
            self.assertEqual(pull["required_approving_review_count"], 1)
            self.assertTrue(pull["dismiss_stale_reviews_on_push"])
            self.assertTrue(pull["require_last_push_approval"])
            self.assertTrue(pull["required_review_thread_resolution"])
            self.assertEqual(pull["allowed_merge_methods"], ["squash"])
            status = normalized["rules"]["required_status_checks"]
            self.assertTrue(status["strict_required_status_checks_policy"])
            self.assertFalse(status["do_not_enforce_on_create"])
            self.assertEqual(
                status["contexts"],
                row["required_status_checks"],
            )

    def test_unknown_repository_fails(self) -> None:
        broken = copy.deepcopy(self.config)
        broken["repositories"][0]["repository"] = "appolon1908-hue/not-authorized"
        with self.assertRaises(MODULE.PolicyError):
            MODULE.validate_config(broken)

    def test_stable_repository_id_drift_fails(self) -> None:
        broken = copy.deepcopy(self.config)
        broken["repositories"][0]["repository_id"] += 1
        with self.assertRaises(MODULE.PolicyError):
            MODULE.validate_config(broken)

    def test_reviewer_drift_fails(self) -> None:
        broken = copy.deepcopy(self.config)
        broken["reviewer"]["permission"] = "admin"
        with self.assertRaises(MODULE.PolicyError):
            MODULE.validate_config(broken)

    def test_validate_mode_is_offline_and_non_mutating(self) -> None:
        document = MODULE.execute("validate", "")
        self.assertEqual(document["result"], "PASS")
        self.assertFalse(document["production_changed"])
        self.assertFalse(document["runtime_contacted"])
        self.assertFalse(document["external_effects_enabled"])
        self.assertEqual(len(document["repositories"]), 4)


if __name__ == "__main__":
    unittest.main()
