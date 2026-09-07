import json
from pathlib import Path
from subprocess import CompletedProcess
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

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
        data = {"pid": 123, "updated_at": time.time(), "writer_alive": True, "write_errors": 0}
        data.update(overrides)
        (self.profile.root / "state/capture.json").write_text(json.dumps(data))

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
        for fields in ({"updated_at": time.time() + 100}, {"pid": None}):
            with self.subTest(fields=fields):
                self.metric(**fields)
                self.assertFalse(doctor(self.profile, self.adapter())["healthy"])

    def test_healthy_observed_profile_stays_healthy(self):
        self.metric()
        result = doctor(self.profile, self.adapter())
        self.assertTrue(result["healthy"])
        self.assertEqual(result["inspection_errors"], {})


if __name__ == "__main__":
    unittest.main()
