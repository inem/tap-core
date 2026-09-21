import importlib.util
from pathlib import Path
import unittest


PATH = Path(__file__).resolve().parents[1] / "tools/check_migration_lifecycle.py"
SPEC = importlib.util.spec_from_file_location("check_migration_lifecycle", PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class MigrationLifecycleCheckTests(unittest.TestCase):
    def test_normalized_network_ignores_disabled_cached_endpoints(self):
        state = {"Wi-Fi": {
            "http": {"enabled": False, "server": "old", "port": 8899},
            "https": {"enabled": True, "server": "127.0.0.1", "port": 18999},
            "bypass": ["localhost"],
        }}
        self.assertEqual(MODULE.normalized_network(state)["Wi-Fi"], {
            "http": {"enabled": False},
            "https": {"enabled": True, "server": "127.0.0.1", "port": 18999},
            "bypass": ["localhost"],
        })

    def test_legacy_detection_only_reports_enabled_8899(self):
        state = {
            "Wi-Fi": {"http": {"enabled": False, "port": 8899},
                        "https": {"enabled": False, "port": 8899}},
            "USB": {"http": {"enabled": True, "port": 8899},
                     "https": {"enabled": True, "port": 18999}},
        }
        self.assertEqual(MODULE.legacy_8899_enabled(state), ["USB"])

    def test_default_report_is_timestamped_under_docs_results(self):
        output = MODULE.default_output()
        self.assertEqual(output.parent, PATH.parents[1] / "docs/results")
        self.assertRegex(output.name, r"^migration-lifecycle-\d{8}T\d{6}Z\.json$")

    def test_doctor_limitations_name_each_failed_gate(self):
        result = {
            "inspection_errors": {"sudoers": "missing"},
            "port_owned": True,
            "capture": {"healthy": True},
            "traffic_probe": True,
            "bridge": {"healthy": True, "enabled": True,
                       "hub_liveness": "live", "control_liveness": "failed"},
            "components": {"healthy": False},
            "system_proxy_verified": True,
        }
        self.assertEqual(MODULE.doctor_limitations(result), {
            "inspection:sudoers", "bridge_liveness", "components"})


if __name__ == "__main__":
    unittest.main()
