from contextlib import redirect_stderr, redirect_stdout
import io
import json
import unittest
from unittest.mock import patch

from tap_core.cli import main
from tap_core.doctor_view import terminal_doctor


def report(**changes):
    value = {
        "profile": "/fixture/profile", "routing": "system", "service_loaded": True,
        "pid": 123, "port_owned": True, "port_open": True,
        "network_recovery_pending": False, "system_proxy_verified": True,
        "inspection_errors": {},
        "capture": {"available": True, "current_process": True, "healthy": True},
        "bridge": {"configured": False, "healthy": True},
        "components": {"configured": False, "healthy": True},
        "backend_version": "12.2.3", "ca_file_present": True,
        "ca_trust": "not_verified; HTTPS clients must trust this profile CA explicitly",
        "sudoers": {"ready": True, "next": None}, "traffic_probe": True,
        "https_decryption": {"probe_url": "https://example.com/",
                             "profile_ca_verified": True,
                             "system_trust_verified": True,
                             "browser_trust": "not_checked; browser trust is separate from curl"},
        "healthy": True,
    }
    value.update(changes)
    if "active_system_proxy_verified" not in changes:
        value["active_system_proxy_verified"] = (
            "not_used" if value["routing"] == "explicit" else value["system_proxy_verified"])
    return value


class DoctorOutputTests(unittest.TestCase):
    def invoke(self, value, *arguments, tty=False):
        class Output(io.StringIO):
            def isatty(self):
                return tty

        out, err = Output(), io.StringIO()
        with patch("tap_core.cli.platform.system", return_value="Darwin"), \
                patch("tap_core.cli.Profile.load"), \
                patch("tap_core.cli.doctor", return_value=value), \
                redirect_stdout(out), redirect_stderr(err):
            code = main(["--profile", "/fixture/profile", "doctor", *arguments])
        return code, out.getvalue(), err.getvalue()

    def test_default_is_human_and_explicit_raw_json_is_unchanged(self):
        value = report()
        code, output, error = self.invoke(value)
        self.assertEqual((code, error), (0, ""))
        self.assertIn("doctor         ● healthy", output)
        self.assertIn("  traffic      ● probe passed", output)
        self.assertIn("  HTTPS decrypt ● profile CA verified", output)
        self.assertIn("  curl trust   ● default trust verified", output)
        self.assertIn("? not verified", output)
        self.assertNotIn('"profile":', output)
        code, raw, error = self.invoke(value, "--output", "raw-json")
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(raw, json.dumps(value, indent=2) + "\n")

    def test_live_https_verification_overrides_missing_grant_metadata(self):
        value = report(ca_trust=("verified_live; profile-CA decryption and default "
                                "/usr/bin/curl trust passed; finish-setup grant record absent"),
                       ca_trust_grant_recorded=False)
        code, output, error = self.invoke(value)
        self.assertEqual((code, error), (0, ""))
        self.assertIn("CA trust     ● verified live", output)
        self.assertIn("CA provenance: live trust may pass, but no owned finish-setup grant is recorded", output)

    def test_failure_keeps_exit_code_and_shows_errors_and_next_once(self):
        value = report(healthy=False, backend_error="backend missing",
                       inspection_errors={"port_open": "denied\x1b[31m"},
                       traffic_probe=False, next=["tap on"])
        value.pop("backend_version")
        code, output, error = self.invoke(value)
        self.assertEqual((code, error), (1, ""))
        self.assertIn("doctor         ✗ needs attention", output)
        self.assertIn("backend missing", output)
        self.assertIn("port_open: denied", output)
        self.assertNotIn("\x1b", output)
        self.assertEqual(output.count("tap on"), 1)
        code, raw, error = self.invoke(value, "--output", "raw-json")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(raw), value)
        self.assertEqual(error, "tap-core: next: tap on\n")

    def test_unhealthy_output_names_false_green_reasons(self):
        value = report(healthy=False, system_proxy_verified=False,
                       bridge={"configured": True, "enabled": True, "healthy": True,
                               "hub_port": 19002, "applies": "startup snapshot",
                               "hub_liveness": "not_checked", "control_liveness": "not_checked"},
                       background={"registered": True, "quarantined": ["tap.intake"],
                                   "verify_failures": {
                                       "tap.intake": {"version": "0.7.8",
                                                      "quarantined": True,
                                                      "error": "__pycache__ changed file set"}
                                   }})
        code, output, error = self.invoke(value)
        self.assertEqual((code, error), (1, ""))
        self.assertIn("Reasons", output)
        self.assertIn("routing: system proxy is not verified", output)
        self.assertIn("Limitations", output)
        self.assertIn("CA trust: not verified", output)
        self.assertIn("bridge: Hub/control liveness was not checked", output)
        self.assertIn("background: quarantined packs: tap.intake", output)
        self.assertIn("pack integrity: quarantined after verify failures: tap.intake@0.7.8", output)

    def test_dead_hub_and_router_are_explicit_actionable_reasons(self):
        value = report(
            healthy=False,
            bridge={"configured": True, "enabled": True, "healthy": True,
                    "hub_liveness": "failed", "control_liveness": "blocked",
                    "liveness_error": "connection refused"},
            components={"configured": True, "healthy": False,
                        "error": "Hub unavailable or hung"})
        code, output, error = self.invoke(value)
        self.assertEqual((code, error), (1, ""))
        self.assertIn("bridge        ✗ control unavailable", output)
        self.assertIn("Hub liveness probe failed: connection refused", output)
        self.assertIn("inspect components.log and use off/on", output)
        self.assertIn("control: managed component control plane is not healthy: Hub unavailable or hung", output)

    def test_https_decryption_and_default_trust_failures_are_actionable(self):
        value = report(healthy=False, https_decryption={
            "profile_ca_verified": True, "system_trust_verified": False,
            "system_trust_error": "SSL certificate problem",
            "browser_trust": "not_checked; browser trust is separate from curl"},
            next=["/owned/checkout/instll/finish-setup"])
        code, output, error = self.invoke(value)
        self.assertEqual((code, error), (1, ""))
        self.assertIn("HTTPS decrypt ● profile CA verified", output)
        self.assertIn("curl trust   ✗ default trust failed", output)
        self.assertIn("curl trust: default trust probe failed: SSL certificate problem", output)
        self.assertIn("/owned/checkout/instll/finish-setup", output)

    def test_unknown_observations_are_called_unknown_not_healthy(self):
        value = report(healthy=False, port_open=None, system_proxy_verified=None,
                       inspection_errors={"port_open": "Operation not permitted",
                                          "system_proxy_verified": "No enabled network services"})
        code, output, error = self.invoke(value)
        self.assertEqual((code, error), (1, ""))
        self.assertIn("runtime: listener inspection is unknown", output)
        self.assertIn("routing: system proxy inspection is unknown", output)
        self.assertIn("Inspection errors", output)
        self.assertIn("port_open: Operation not permitted", output)

    def test_active_service_rescue_names_mismatches_and_repair_policy(self):
        disabled = {"enabled": False, "server": "", "port": 0}
        value = report(
            healthy=False, system_proxy_verified=False,
            active_system_proxy_verified=True,
            active_network_service="USB 10/100/1000 LAN",
            system_proxy_mismatches=[
                {"service": "Wi-Fi", "http": disabled, "https": disabled},
            ])
        code, output, error = self.invoke(value)
        self.assertEqual((code, error), (1, ""))
        self.assertIn("active service USB 10/100/1000 LAN works", output)
        self.assertIn("Wi-Fi (HTTP off, HTTPS off)", output)
        self.assertIn("rescue route is not owned", output)

    def test_color_auto_is_tty_only(self):
        value = report()
        self.assertNotIn("\x1b[", self.invoke(value)[1])
        self.assertIn("\x1b[", self.invoke(value, tty=True)[1])
        self.assertNotIn("\x1b[", self.invoke(value, "--color", "never", tty=True)[1])
        self.assertIn("\x1b[", self.invoke(value, "--color", "always")[1])


if __name__ == "__main__":
    unittest.main()
