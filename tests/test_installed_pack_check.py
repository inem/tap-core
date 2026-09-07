"""Negative coverage for the #14 live-check gate: cleanup success must not imply
scenario success. No launchd, no network — pure assertions over the gate."""
import unittest

from tools.check_installed_pack import scenario_failures


def passing_steps():
    return {
        "install": "ok",
        "pack_install": {"id": "tap.check.installed-page", "version": "0.1.0"},
        "pack_enable": "ok",
        "on": "ok",
        "injection_granted_marker": True,
        "installed_resource_served": True,
        "exclusion_suppresses_injection": True,
        "bridge_explain": {"granted_allowed": True, "excluded_reason": "user_exclusion", "excluded_blocked": True},
        "pack_disable": {"enabled": False},
        "pack_uninstall": {"removed_versions": ["0.1.0"]},
        "data_sentinel_retained": True,
    }


class ScenarioGateTests(unittest.TestCase):
    def test_fully_passing_scenario_has_no_failures(self):
        self.assertEqual(scenario_failures(passing_steps()), [])

    def test_empty_scenario_fails_every_claim(self):
        self.assertGreaterEqual(len(scenario_failures({})), 10)

    def test_uninjected_granted_origin_fails(self):
        steps = passing_steps()
        steps["injection_granted_marker"] = False
        self.assertIn("injection_granted_marker", " ".join(scenario_failures(steps)))

    def test_wrong_resource_bytes_fail(self):
        steps = passing_steps()
        steps["installed_resource_served"] = False
        self.assertIn("installed_resource_served", " ".join(scenario_failures(steps)))

    def test_excluded_origin_still_injected_fails(self):
        steps = passing_steps()
        steps["exclusion_suppresses_injection"] = False
        self.assertIn("exclusion_suppresses_injection", " ".join(scenario_failures(steps)))

    def test_wrong_exclusion_reason_fails(self):
        steps = passing_steps()
        steps["bridge_explain"]["excluded_reason"] = "WRONG"
        self.assertIn("explain_exclusion", " ".join(scenario_failures(steps)))

    def test_lost_retained_data_fails(self):
        steps = passing_steps()
        steps["data_sentinel_retained"] = False
        self.assertIn("data_retained", " ".join(scenario_failures(steps)))

    def test_uninstall_without_version_fails(self):
        steps = passing_steps()
        steps["pack_uninstall"] = {"removed_versions": []}
        self.assertIn("pack_uninstall", " ".join(scenario_failures(steps)))


if __name__ == "__main__":
    unittest.main()
