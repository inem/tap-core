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
        "healthy": True,
    }
    value.update(changes)
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
        self.assertIn("? not verified", output)
        self.assertNotIn('"profile":', output)
        code, raw, error = self.invoke(value, "--output", "raw-json")
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(raw, json.dumps(value, indent=2) + "\n")

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

    def test_color_auto_is_tty_only(self):
        value = report()
        self.assertNotIn("\x1b[", self.invoke(value)[1])
        self.assertIn("\x1b[", self.invoke(value, tty=True)[1])
        self.assertNotIn("\x1b[", self.invoke(value, "--color", "never", tty=True)[1])
        self.assertIn("\x1b[", self.invoke(value, "--color", "always")[1])


if __name__ == "__main__":
    unittest.main()
