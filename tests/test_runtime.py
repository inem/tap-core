import copy
import json
from pathlib import Path
import plistlib
import shlex
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch, PropertyMock

from tap_core.capture import Capture, Writer, wants_body
from tap_core.runtime import Lifecycle, MacOS, Profile, TapError


class NetworkFixture(MacOS):
    """Only OS boundaries replaced; real arm/disarm/lifecycle code executes."""
    def __init__(self):
        disabled = {"enabled": False, "server": "previous.test", "port": 8000}
        self.network = {name: {"http": dict(disabled), "https": dict(disabled), "bypass": ["*.internal"]}
                        for name in ("Wi-Fi", "USB Ethernet")}
        self.events = []
        self.fail_arm = self.fail_restore = self.fail_start = self.fail_probe = False
        self.start_calls = 0

    def services(self):
        return list(self.network)

    def proxy(self, name, secure=False):
        return dict(self.network[name]["https" if secure else "http"])

    def bypass(self, name):
        return list(self.network[name]["bypass"])

    def set_proxy(self, name, secure, state):
        self.events.append(("set", name, secure, state["enabled"]))
        if self.fail_arm and name == "USB Ethernet" and secure and state["enabled"]:
            raise TapError("fixture partial arm failure")
        if self.fail_restore and not state["enabled"]:
            raise TapError("fixture restore failure")
        kind = "https" if secure else "http"
        if state["enabled"]:
            self.network[name][kind] = dict(state)
        else:
            self.network[name][kind]["enabled"] = False

    def set_bypass(self, name, values):
        self.network[name]["bypass"] = list(values)

    def backend_version(self, profile):
        return "12.2.3"

    def port_open(self, profile):
        return False

    def start(self, profile):
        self.start_calls += 1
        self.events.append("start")
        if self.fail_start:
            raise TapError("fixture start failure")

    def stop(self, profile):
        self.events.append("stop")

    def flows(self, profile):
        self.events.append("probe")
        return not self.fail_probe


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.profile = Profile(self.root, "/fixture/mitmdump", 18999, "system", "http://example.test", [])
        self.profile.save()
        self.os = NetworkFixture()
        self.before = copy.deepcopy(self.os.network)
        self.runtime = Lifecycle(self.profile, self.os)

    def assertRestored(self):
        for service, state in self.before.items():
            self.assertEqual(self.os.network[service]["bypass"], state["bypass"])
            for kind in ("http", "https"):
                self.assertEqual(self.os.network[service][kind]["enabled"], state[kind]["enabled"])

    def test_disable_does_not_reenable_a_previous_endpoint(self):
        adapter = MacOS()
        with patch.object(adapter, "run") as run:
            adapter.set_proxy("Wi-Fi", False, {"enabled": False, "server": "previous.test", "port": 8000})
        run.assert_called_once_with(["/usr/bin/sudo", "-n", "/usr/sbin/networksetup", "-setwebproxystate", "Wi-Fi", "off"])

    def test_system_on_off_restores_all_services_and_bypass_before_stop(self):
        self.runtime.on()
        self.assertTrue(self.os.armed(self.profile))
        self.assertTrue(self.profile.snapshot.exists())
        self.assertEqual(self.os.events[0], "start")
        self.assertEqual(self.os.events[-1], "probe")
        self.runtime.off()
        self.assertRestored()
        self.assertEqual(self.os.events[-1], "stop")
        self.assertFalse(self.profile.snapshot.exists())

    def test_partial_arm_failure_rolls_back_inactive_service_too(self):
        self.os.fail_arm = True
        with self.assertRaisesRegex(TapError, "previous proxy routing restored"):
            self.runtime.on()
        self.assertRestored()
        self.assertNotIn("probe", self.os.events)

    def test_failed_rollback_keeps_snapshot_and_does_not_claim_safety(self):
        self.os.fail_arm = self.os.fail_restore = True
        with self.assertRaisesRegex(TapError, "rollback FAILED"):
            self.runtime.on()
        self.assertTrue(self.profile.snapshot.exists())
        self.assertNotIn("stop", self.os.events)

    def test_off_does_not_stop_after_partial_restore_failure(self):
        self.runtime.on()
        self.os.fail_restore = True
        with self.assertRaisesRegex(TapError, "recovery incomplete"):
            self.runtime.off()
        self.assertNotIn("stop", self.os.events)

    def test_probe_failure_restores_network(self):
        self.os.fail_probe = True
        with self.assertRaisesRegex(TapError, "previous proxy routing restored"):
            self.runtime.on()
        self.assertRestored()

    def test_start_failure_does_not_arm(self):
        self.os.fail_start = True
        with self.assertRaises(TapError):
            self.runtime.on()
        self.assertEqual(self.os.events, ["start"])

    def test_install_failure_is_an_error(self):
        self.os.fail_start = True
        with self.assertRaisesRegex(TapError, "Installation failed"):
            self.runtime.install()

    def test_crash_restart_failure_recovers_already_armed_network(self):
        self.runtime.on()
        self.os.fail_start = True
        with self.assertRaisesRegex(TapError, "previous proxy routing restored"):
            self.runtime.on()
        self.assertRestored()

    def test_existing_proxy_is_not_replaced(self):
        self.os.network["Wi-Fi"]["http"]["enabled"] = True
        before = copy.deepcopy(self.os.network)
        with self.assertRaisesRegex(TapError, "existing system proxy"):
            self.runtime.on()
        self.assertEqual(self.os.network, before)
        self.assertFalse(self.profile.snapshot.exists())

    def test_missing_backend_recovers_previously_armed_routing(self):
        self.runtime.on()
        with patch.object(self.os, "backend_version", side_effect=TapError("backend missing")):
            with self.assertRaisesRegex(TapError, "previous proxy routing restored"):
                self.runtime.on()
        self.assertRestored()

    def test_repeat_on_preserves_original_snapshot(self):
        self.runtime.on()
        before = self.profile.snapshot.read_bytes()
        self.runtime.on()
        self.assertEqual(self.profile.snapshot.read_bytes(), before)
        self.runtime.off()
        self.assertRestored()

    def test_repeat_on_verifies_all_saved_bypass_lists(self):
        self.runtime.on()
        self.os.network["USB Ethernet"]["bypass"] = ["*.internal"]
        with self.assertRaisesRegex(TapError, "needs recovery"):
            self.runtime.on()
        self.assertEqual(self.os.events.count("probe"), 1)
        self.assertRestored()

    def test_save_tightens_existing_profile_directories(self):
        paths = [self.profile.root, *(self.profile.root / name for name in ("data", "state", "certificates", "logs"))]
        for path in paths:
            path.chmod(0o755)
        self.profile.save()
        self.assertTrue(all(path.stat().st_mode & 0o777 == 0o700 for path in paths))

    def test_save_rejects_external_directory_symlinks(self):
        outside = self.root / "outside"
        outside.mkdir(mode=0o755)
        outside.chmod(0o755)
        data = self.profile.root / "data"
        data.rmdir()
        data.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(TapError, "must not be a symlink"):
            self.profile.save()
        self.assertEqual(outside.stat().st_mode & 0o777, 0o755)

    def test_off_refuses_to_overwrite_a_subsequently_changed_proxy(self):
        self.runtime.on()
        self.os.network["Wi-Fi"]["http"]["server"] = "another-proxy.test"
        before = copy.deepcopy(self.os.network)
        with self.assertRaisesRegex(TapError, "changed outside"):
            self.runtime.off()
        self.assertEqual(self.os.network, before)
        self.assertNotIn("stop", self.os.events)

    def test_off_checks_newly_added_service_before_stopping(self):
        self.runtime.on()
        self.os.network["New service"] = copy.deepcopy(self.os.network["Wi-Fi"])
        with self.assertRaisesRegex(TapError, "New service"):
            self.runtime.off()
        self.assertTrue(self.profile.snapshot.exists())
        self.assertNotIn("stop", self.os.events)

    def test_no_snapshot_but_our_proxy_enabled_refuses_stop(self):
        self.runtime.on()
        self.profile.snapshot.unlink()
        with self.assertRaisesRegex(TapError, "snapshot is missing"):
            self.runtime.off()
        self.assertNotIn("stop", self.os.events)

    def test_explicit_profile_never_accesses_network_settings(self):
        self.profile.routing = "explicit"
        with patch.object(self.os, "services", side_effect=AssertionError("system settings accessed")):
            self.runtime.on()
            output = self.runtime.off()
        self.assertIn("explicit clients", output)
        self.assertEqual(self.os.events, ["start", "probe", "stop"])

    def test_only_http_enabled_is_not_armed(self):
        self.runtime.on()
        self.os.network["USB Ethernet"]["https"]["enabled"] = False
        self.assertFalse(self.os.armed(self.profile))

    def test_profile_roundtrip_and_unique_service_label(self):
        self.assertEqual(Profile.load(self.root), self.profile)
        other = Profile(self.root / "other", "/fixture/mitmdump", 19000, "explicit", "http://example.test", [])
        self.assertNotEqual(other.label, self.profile.label)

    def test_plist_escapes_paths_and_preserves_launchd_settings(self):
        self.profile.backend = "/fixture/a 'quote' & space/mitmdump"
        with patch.object(Profile, "plist", new_callable=PropertyMock, return_value=self.root / "agent.plist"):
            MacOS().write_plist(self.profile)
            plist = plistlib.loads(self.profile.plist.read_bytes())
        self.assertTrue(plist["KeepAlive"] and plist["RunAtLoad"])
        wrapper = plist["ProgramArguments"][2]
        self.assertTrue(wrapper.startswith("ulimit -n 65536 || exit; exec "))
        argv = shlex.split(wrapper.split("; exec ", 1)[1])
        self.assertEqual(argv[0], self.profile.backend)
        self.assertIn("confdir=" + str(self.profile.root / "certificates"), argv)
        self.assertIn("127.0.0.1", argv)

    def test_real_start_does_not_touch_foreign_listener(self):
        adapter = MacOS()
        with patch.object(adapter, "owns_port", return_value=False), patch.object(adapter, "port_open", return_value=True), patch.object(adapter, "run") as run:
            with self.assertRaisesRegex(TapError, "occupied"):
                adapter.start(self.profile)
            run.assert_not_called()

    def test_plist_write_failure_prevents_bootstrap(self):
        adapter = MacOS()
        with patch.object(adapter, "owns_port", return_value=False), patch.object(adapter, "port_open", return_value=False), patch.object(adapter, "service_loaded", return_value=False), patch.object(adapter, "write_plist", side_effect=OSError("disk full")), patch.object(adapter, "run") as run:
            with self.assertRaises(OSError):
                adapter.start(self.profile)
            run.assert_not_called()


class CaptureTests(unittest.TestCase):
    def test_media_types_are_case_insensitive(self):
        for ctype in ("Application/JSON", "TEXT/HTML; charset=UTF-8", "application/vnd.test+JSON"):
            self.assertTrue(wants_body(ctype))
        self.assertFalse(wants_body("Text/Event-Stream"))
        self.assertFalse(wants_body("Application/Octet-Stream"))

    def test_mixed_case_json_is_captured(self):
        records = []
        capture = Capture(SimpleNamespace(submit=records.append))
        request = SimpleNamespace(method="GET", url="http://example.test/", headers={}, stream=False,
                                  get_text=lambda **kwargs: "")
        response = SimpleNamespace(status_code=200, headers={"content-type": "Application/JSON", "content-length": "2"},
                                   stream=False, get_text=lambda **kwargs: "{}")
        flow = SimpleNamespace(request=request, response=response)
        capture.responseheaders(flow)
        capture.response(flow)
        self.assertFalse(response.stream)
        self.assertEqual(records[0]["body"], "{}")

    def test_binary_sse_and_large_streamed_json_do_not_read_bodies(self):
        class Response:
            status_code = 200
            stream = False
            def __init__(self, ctype):
                self.headers = {"content-type": ctype, "content-length": "9000000"}
            @property
            def raw_content(self):
                raise AssertionError("streamed body accessed")
            def get_text(self, **kwargs):
                raise AssertionError("streamed body decoded")
        records = []
        capture = Capture(SimpleNamespace(submit=records.append))
        for ctype in ("application/octet-stream", "text/event-stream", "application/json"):
            response = Response(ctype)
            if ctype == "application/json":
                response.stream = True  # backend stream_large_bodies behavior
            request = SimpleNamespace(method="GET", url="http://example.test/", headers={})
            flow = SimpleNamespace(response=response, request=request)
            capture.responseheaders(flow)
            capture.response(flow)
        self.assertEqual(len(records), 3)
        self.assertTrue(all(record["streamed"] and not record["body_kept"] and "body" not in record for record in records))

    def test_rotation_does_not_reset_consumer_offset_and_closes_writer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "read.offset").write_text("123")
            writer = Writer(root, root, max_bytes=90, keep_rolls=2)
            for index in range(8):
                writer.submit({"index": index, "body": "x" * 50})
            writer.close()
            self.assertFalse(writer.thread.is_alive())
            self.assertEqual(writer.written, 8)
            self.assertEqual((root / "read.offset").read_text(), "123")
            self.assertEqual(len(list(root.glob("stream.jsonl.*"))), 2)
            self.assertEqual(json.loads((root / "stream.jsonl").read_text())["index"], 7)

    def test_writer_errors_are_visible(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            writer = Writer(root / "missing", root)
            writer.submit({"body": "fixture"})
            writer.close()
            report = json.loads((root / "capture.json").read_text())
            self.assertEqual(report["write_errors"], 1)
            self.assertFalse(report["writer_alive"])


if __name__ == "__main__":
    unittest.main()
