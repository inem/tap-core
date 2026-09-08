import json
from pathlib import Path
from subprocess import CompletedProcess
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
import os

from tap_core.capture import Writer
from tap_core.cli import doctor, status
from tap_core.runtime import MacOS, Profile, TapError


class ObservationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.profile = Profile(Path(directory.name), "/fixture/mitmdump", 18999, "explicit", "http://example.test/", [])
        self.profile.save()

    def test_missing_service_is_known_absence(self):
        result = CompletedProcess([], 113, "", f'Bad request.\nCould not find service "{self.profile.label}" in domain for user gui: 501\n')
        adapter = MacOS()
        with patch.object(adapter, "run", return_value=result):
            self.assertFalse(adapter.service_loaded(self.profile))
            self.assertIsNone(adapter.service_pid(self.profile))

    def test_permission_and_unknown_domain_errors_are_not_absence(self):
        adapter = MacOS()
        for code, text in ((1, "Operation not permitted"), (113, "Could not find domain for user gui: 501")):
            with self.subTest(text=text), patch.object(adapter, "run", return_value=CompletedProcess([], code, "", text)):
                with self.assertRaisesRegex(TapError, "Cannot inspect"):
                    adapter.service_loaded(self.profile)
                with self.assertRaisesRegex(TapError, "Cannot inspect"):
                    adapter.service_pid(self.profile)

    def test_listener_permission_failure_is_not_closed_port(self):
        with patch("tap_core.runtime.socket.create_connection", side_effect=PermissionError("denied")):
            with self.assertRaisesRegex(TapError, "Cannot inspect listener"):
                MacOS().port_open(self.profile)

    def test_connection_refused_is_closed_port(self):
        with patch("tap_core.runtime.socket.create_connection", side_effect=ConnectionRefusedError()):
            self.assertFalse(MacOS().port_open(self.profile))

    def test_lsof_warning_does_not_prove_complete_owner_set(self):
        adapter = MacOS()
        with patch.object(adapter, "service_pid", return_value=123), patch.object(adapter, "run", return_value=CompletedProcess([], 0, "123\n", "permission warning")):
            with self.assertRaisesRegex(TapError, "Cannot inspect listener ownership"):
                adapter.owns_port(self.profile)

    def test_empty_lsof_result_is_known_absence(self):
        adapter = MacOS()
        with patch.object(adapter, "service_pid", return_value=123), patch.object(adapter, "run", return_value=CompletedProcess([], 1, "", "")):
            self.assertFalse(adapter.owns_port(self.profile))

    def test_nonzero_lsof_with_output_is_unknown_even_if_pid_matches(self):
        adapter = MacOS()
        for stdout in ("123\n", "456\n", "123\n456\n", "\n"):
            with self.subTest(stdout=stdout), patch.object(adapter, "service_pid", return_value=123), patch.object(adapter, "run", return_value=CompletedProcess([], 1, stdout, "")):
                with self.assertRaisesRegex(TapError, "Cannot inspect listener ownership"):
                    adapter.owns_port(self.profile)

    def test_successful_complete_lsof_preserves_known_owner_results(self):
        adapter = MacOS()
        for stdout, owned in (("123\n", True), ("456\n", False), ("123\n456\n", False)):
            with self.subTest(stdout=stdout), patch.object(adapter, "service_pid", return_value=123), patch.object(adapter, "run", return_value=CompletedProcess([], 0, stdout, "")):
                self.assertIs(adapter.owns_port(self.profile), owned)

    def adapter(self):
        adapter = Mock(spec=MacOS)
        adapter.service_loaded.return_value = True
        adapter.service_pid.return_value = 123
        adapter.owns_port.return_value = True
        adapter.port_open.return_value = True
        adapter.backend_version.return_value = "12.2.3"
        adapter.flows.return_value = True
        return adapter

    def metric(self, **overrides):
        data = {"pid": 123, "updated_at": time.time(), "writer_alive": True,
                "written": 0, "dropped": 0, "write_errors": 0, "last_error": None, "queued_bytes": 0}
        data.update(overrides)
        (self.profile.root / "state/capture.json").write_text(json.dumps(data))
        return data

    def assert_capture_unknown(self, adapter=None):
        result = doctor(self.profile, adapter or self.adapter())
        self.assertEqual(result["capture"], {"available": None, "healthy": None, "current_process": None})
        self.assertIn("capture", result["inspection_errors"])
        self.assertIs(result["healthy"], False)
        self.assertIs(result["port_open"], True)
        return result

    def test_missing_health_is_known_absence_without_an_inspection_error(self):
        result = doctor(self.profile, self.adapter())
        self.assertEqual(result["capture"], {"available": False, "healthy": False, "current_process": False})
        self.assertNotIn("capture", result["inspection_errors"])
        self.assertIs(result["healthy"], False)

    def test_health_read_permission_failure_is_unknown_not_absent(self):
        self.metric()
        original = Path.read_text

        def read_text(path, *args, **kwargs):
            if path == self.profile.root / "state/capture.json":
                raise PermissionError("fixture permission denied")
            return original(path, *args, **kwargs)

        with patch.object(Path, "read_text", read_text):
            result = self.assert_capture_unknown()
        self.assertIn("permission denied", result["inspection_errors"]["capture"])

    def test_other_health_read_errors_are_unknown(self):
        path = self.profile.root / "state/capture.json"
        path.mkdir()
        result = self.assert_capture_unknown()
        self.assertIn("Cannot read", result["inspection_errors"]["capture"])

    def test_malformed_json_and_invalid_utf8_are_unknown(self):
        path = self.profile.root / "state/capture.json"
        for data in (b'{"pid":', b'\xff'):
            with self.subTest(data=data):
                path.write_bytes(data)
                result = self.assert_capture_unknown()
                self.assertIn("Invalid capture health JSON", result["inspection_errors"]["capture"])

    def test_non_object_health_records_are_unknown(self):
        path = self.profile.root / "state/capture.json"
        for data in (None, [], True, "record"):
            with self.subTest(data=data):
                path.write_text(json.dumps(data))
                self.assert_capture_unknown()

    def test_writer_boolean_and_counter_types_cannot_establish_health(self):
        for field, values in (
                ("writer_alive", ("yes", "false", 1, 0, None, [], {})),
                ("pid", (True, 0, -1, 123.0, "123", None)),
                ("written", (True, False, -1, 0.0, "0", None)),
                ("dropped", (True, False, -1, 0.0, "0", None)),
                ("write_errors", (True, False, -1, 0.0, "0", None)),
                ("queued_bytes", (True, False, -1, 0.0, "0", None)),
                ("last_error", (False, 0, [], {}))):
            for value in values:
                with self.subTest(field=field, value=value):
                    self.metric(**{field: value})
                    result = self.assert_capture_unknown()
                    self.assertIn(field, result["inspection_errors"]["capture"])

    def test_invalid_timestamp_types_and_nonfinite_times_are_unknown(self):
        for value in (True, False, "now", None, [], {}, 0, -1, float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                self.metric(updated_at=value)
                result = self.assert_capture_unknown()
                self.assertIn("updated_at", result["inspection_errors"]["capture"])

    def test_missing_writer_metrics_are_unknown(self):
        keys = self.metric().keys()
        for name in keys:
            with self.subTest(name=name):
                data = self.metric()
                del data[name]
                (self.profile.root / "state/capture.json").write_text(json.dumps(data))
                result = self.assert_capture_unknown()
                self.assertIn(name, result["inspection_errors"]["capture"])

    def test_capture_owner_permission_failure_gives_unknown_capture_values(self):
        self.metric()
        adapter = self.adapter()
        adapter.service_pid.side_effect = TapError("fixture launchctl permission denied")
        result = self.assert_capture_unknown(adapter)
        self.assertIsNone(result["pid"])
        self.assertIn("pid", result["inspection_errors"])

    def test_valid_dead_or_erroring_writer_is_known_unhealthy(self):
        for fields in ({"writer_alive": False}, {"write_errors": 1, "last_error": "disk full"}):
            with self.subTest(fields=fields):
                self.metric(**fields)
                result = doctor(self.profile, self.adapter())
                self.assertIs(result["capture"]["available"], True)
                self.assertIs(result["capture"]["healthy"], False)
                self.assertIs(result["healthy"], False)
                self.assertEqual(result["inspection_errors"], {})

    def test_actual_writer_health_fields_are_accepted(self):
        writer = Writer(self.profile.root / "data", self.profile.root / "state")
        self.addCleanup(writer.close)
        writer.submit({"id": "fixture"})
        deadline = time.monotonic() + 2
        while True:
            try:
                data = json.loads((self.profile.root / "state/capture.json").read_text())
            except FileNotFoundError:
                data = {}
            if data.get("written") == 1:
                break
            self.assertLess(time.monotonic(), deadline, "Writer did not publish fixture health")
            time.sleep(0.01)
        adapter = self.adapter()
        adapter.service_pid.return_value = data["pid"]
        result = doctor(self.profile, adapter)
        self.assertEqual(result["inspection_errors"], {})
        self.assertIs(result["capture"]["available"], True)
        self.assertEqual(result["capture"]["written"], 1)
        self.assertIs(result["capture"]["writer_alive"], True)
        self.assertIs(result["healthy"], True)
        writer.close()
        self.assertFalse(writer.thread.is_alive())
        result = doctor(self.profile, adapter)
        self.assertEqual(result["inspection_errors"], {})
        self.assertIs(result["capture"]["writer_alive"], False)
        self.assertIs(result["healthy"], False)

    def test_status_keeps_independent_checks_when_one_is_unavailable(self):
        adapter = self.adapter()
        adapter.owns_port.side_effect = TapError("cannot inspect owners")
        result = status(self.profile, adapter)
        self.assertIsNone(result["port_owned"])
        self.assertTrue(result["port_open"])
        self.assertIn("port_owned", result["inspection_errors"])
        adapter.flows.assert_not_called()

    def test_doctor_does_not_probe_an_unknown_owner_or_report_healthy(self):
        adapter = self.adapter()
        adapter.owns_port.side_effect = TapError("cannot inspect owners")
        self.metric()
        result = doctor(self.profile, adapter)
        self.assertFalse(result["healthy"])
        self.assertIsNone(result["traffic_probe"])
        adapter.flows.assert_not_called()

    def test_probe_execution_error_is_structured(self):
        adapter = self.adapter()
        adapter.flows.side_effect = TapError("curl could not execute")
        self.metric()
        result = doctor(self.profile, adapter)
        self.assertFalse(result["healthy"])
        self.assertIsNone(result["traffic_probe"])
        self.assertIn("traffic_probe", result["inspection_errors"])

    def test_future_and_invalid_health_records_are_not_healthy(self):
        for fields in ({"updated_at": time.time() + 100}, {"updated_at": 10 ** 1000},
                       {"updated_at": time.time() - 10}, {"pid": None}):
            with self.subTest(fields=fields):
                self.metric(**fields)
                self.assertFalse(doctor(self.profile, self.adapter())["healthy"])

    def test_healthy_observed_profile_stays_healthy(self):
        self.metric()
        result = doctor(self.profile, self.adapter())
        self.assertTrue(result["healthy"])
        self.assertEqual(result["inspection_errors"], {})

    def test_doctor_prints_absolute_finish_setup_when_ca_grant_missing(self):
        import base64
        import hashlib
        import re
        import subprocess
        from tap_core.cli import finish_setup_command, finish_setup_next, path_export_command

        parent = tempfile.TemporaryDirectory()
        self.addCleanup(parent.cleanup)
        root = Path(parent.name)
        profile_dir = root / "profile"
        profile_dir.mkdir()
        checkout = root / "checkout" / "instll"
        checkout.mkdir(parents=True)
        (checkout / "finish-setup").write_text("#!/bin/bash\n")
        bin_dir = root / "bin"
        bin_dir.mkdir()
        wrapper = bin_dir / "tap"
        wrapper.write_text("#!/bin/sh\n")
        (root / "install.json").write_text(json.dumps({
            "version": 1, "grants": {}, "wrapper": str(wrapper)}))
        cert_dir = profile_dir / "certificates"
        cert_dir.mkdir()
        pem = cert_dir / "mitmproxy-ca-cert.pem"
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-keyout", str(cert_dir / "key.pem"),
             "-out", str(pem), "-days", "1", "-nodes", "-subj", "/CN=tap-fixture"],
            check=True, capture_output=True)
        profile = Profile(profile_dir, "/fixture/mitmdump", 18999, "explicit", "http://example.test/", [])
        profile.save()
        data = {"pid": 123, "updated_at": time.time(), "writer_alive": True,
                "written": 0, "dropped": 0, "write_errors": 0, "last_error": None, "queued_bytes": 0}
        (profile_dir / "state").mkdir(exist_ok=True)
        (profile_dir / "state/capture.json").write_text(json.dumps(data))
        expected = [path_export_command(root), finish_setup_command(root)]
        result = doctor(profile, self.adapter())
        self.assertEqual(result["next"], expected)
        self.assertIn("not_verified", result["ca_trust"])
        self.assertEqual(finish_setup_next(profile), expected[1])
        text = pem.read_text()
        match = re.search(r"-----BEGIN CERTIFICATE-----(.*?)-----END CERTIFICATE-----", text, re.S)
        digest = hashlib.sha256(base64.b64decode("".join(match.group(1).split()))).hexdigest()
        (root / "install.json").write_text(json.dumps({
            "version": 1, "wrapper": str(wrapper),
            "grants": {"ca": {"cert": str(pem), "sha256": digest}}}))
        self.assertIsNone(finish_setup_next(profile))
        # PATH still missing from this process → keep export-only next.
        result = doctor(profile, self.adapter())
        self.assertEqual(result["next"], [path_export_command(root)])
        self.assertIn("granted", result["ca_trust"])
        # When bin is already on PATH, no next.
        with patch.dict(os.environ, {"PATH": str(bin_dir.resolve()) + os.pathsep + os.environ.get("PATH", "")}):
            result = doctor(profile, self.adapter())
        self.assertNotIn("next", result)


if __name__ == "__main__":
    unittest.main()
