"""Retention policy reloads into a running capture addon without restart (#247)."""
import base64
import json
import os
from pathlib import Path
import plistlib
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import PropertyMock, patch

from tap_core.capture import (POLICY_EVENTS, POLICY_FILE, Capture, PolicyWatcher,
                              apply_policy_document, capture_limits, policy_document,
                              policy_revision)
from tap_core.capture_policy import reload, write_policy
from tap_core.runtime import MacOS, Profile, TapError

GROK = "https://api2.cursor.sh/aiserver.v1.GrokBotService/ListGrokBotTranscriptEntries"
PROTO = b"\x08\x01\x12\x05hello"


def proto_flow(url=GROK):
    response = SimpleNamespace(status_code=200, headers={"content-type": "application/proto"},
                               stream=False, raw_content=PROTO,
                               get_content=lambda strict=True: PROTO,
                               get_text=lambda **kw: PROTO.decode("utf-8", "replace"))
    request = SimpleNamespace(method="POST", url=url, headers={}, stream=False,
                              get_text=lambda **kw: "")
    return SimpleNamespace(request=request, response=response)


def run_flow(capture, flow):
    capture.responseheaders(flow)
    capture.response(flow)


def until(predicate, timeout=5):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return bool(predicate())


class PolicyDocumentTests(unittest.TestCase):
    def test_document_roundtrips_and_only_changes_retention_keys(self):
        base = capture_limits()
        desired = dict(base, binary_paths=(GROK,), request_body_paths=("/v1/x",))
        updated = apply_policy_document(policy_document(desired), base)
        self.assertEqual(updated["binary_paths"], (GROK,))
        self.assertEqual(updated["request_body_paths"], ("/v1/x",))
        self.assertEqual({k: v for k, v in updated.items() if k not in ("binary_paths", "request_body_paths")},
                         {k: v for k, v in base.items() if k not in ("binary_paths", "request_body_paths")})
        self.assertEqual(base["binary_paths"], ())  # never mutated

    def test_restart_only_and_invalid_documents_are_rejected(self):
        base = capture_limits()
        good = policy_document(dict(base, binary_paths=(GROK,)))
        cases = {
            "restart-only keys: segment_bytes": dict(good, segment_bytes=1),
            "version 1": dict(good, version=2),
            "revision does not match": dict(good, revision="0" * 16),
            "binary_paths must be a list": dict(good, binary_paths=[""]),
            "requires keys": {k: v for k, v in good.items() if k != "request_body_paths"},
        }
        for message, document in cases.items():
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                apply_policy_document(document, base)


class PolicyWatcherTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name) / "state"
        self.data = Path(tmp.name) / "data"
        self.state.mkdir()
        self.data.mkdir()
        self.records = []
        self.writer = SimpleNamespace(submit=self.records.append, policy_status=None)
        self.capture = Capture(self.writer, limits=capture_limits())
        self.watcher = PolicyWatcher(self.capture, self.state, self.data)
        self.watcher.started()

    def events(self):
        return [json.loads(line) for line in (self.data / POLICY_EVENTS).read_text().splitlines()]

    def test_applied_reload_swaps_patterns_and_is_observable(self):
        run_flow(self.capture, proto_flow())
        self.assertFalse(self.records[-1]["body_kept"])
        desired = dict(self.capture.limits, binary_paths=(GROK,))
        write_policy(self.state / POLICY_FILE, policy_document(desired))
        with patch("builtins.print") as printed:
            self.assertTrue(self.watcher.check())
        run_flow(self.capture, proto_flow())
        self.assertTrue(self.records[-1]["body_kept"])
        self.assertEqual(base64.b64decode(self.records[-1]["body"]), PROTO)
        revision = policy_revision(desired)
        self.assertEqual(self.writer.policy_status["revision"], revision)
        self.assertEqual(self.writer.policy_status["source"], "reload")
        self.assertIn(f"capture policy applied revision={revision}", printed.call_args[0][0])
        startup, applied = self.events()
        self.assertEqual((startup["event"], startup["source"]), ("capture.policy.applied", "startup"))
        self.assertEqual(applied["changes"]["binary_paths"], {"added": [GROK], "removed": []})
        self.assertEqual(applied["previous"], startup["revision"])
        # Same file again is a no-op, not a second event.
        self.assertIsNone(self.watcher.check())
        self.assertEqual(len(self.events()), 2)

    def test_invalid_reload_keeps_previous_policy_and_reports(self):
        before = self.capture.limits
        (self.state / POLICY_FILE).write_text(json.dumps(
            dict(policy_document(dict(before, binary_paths=(GROK,))), keep_rolls=0)))
        with patch("builtins.print"):
            self.assertFalse(self.watcher.check())
        self.assertIs(self.capture.limits, before)
        self.assertIn("restart-only keys: keep_rolls", self.writer.policy_status["error"])
        self.assertEqual(self.writer.policy_status["revision"], policy_revision(before))
        self.assertEqual(self.events()[-1]["event"], "capture.policy.rejected")
        (self.state / POLICY_FILE).write_text("{not json")
        with patch("builtins.print"):
            self.assertFalse(self.watcher.check())
        self.assertIs(self.capture.limits, before)

    def test_flow_in_progress_keeps_header_time_decision(self):
        flow = proto_flow()
        self.capture.responseheaders(flow)  # old policy: dropped and streamed
        write_policy(self.state / POLICY_FILE,
                     policy_document(dict(self.capture.limits, binary_paths=(GROK,))))
        with patch("builtins.print"):
            self.watcher.check()
        flow.response.stream = True
        self.capture.response(flow)
        self.assertFalse(self.records[-1]["body_kept"])
        self.assertEqual(self.records[-1]["body_reason"], "media_type")


class LoadedAddonTests(unittest.TestCase):
    def test_loaded_addon_publishes_and_applies_reload_without_traffic(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "state").mkdir()
            (root / "data").mkdir()
            env = {"TAP_CORE_DATA": str(root / "data"), "TAP_CORE_STATE": str(root / "state"),
                   "TAP_CORE_CAPTURE": json.dumps(dict(capture_limits(), binary_paths=[]))}
            with patch.dict(os.environ, env), patch("builtins.print"):
                capture = Capture()
                capture.load(None)
                try:
                    health = root / "state/capture.json"

                    def policy():
                        try:
                            return json.loads(health.read_text()).get("policy") or {}
                        except (OSError, ValueError):
                            return {}
                    self.assertTrue(until(lambda: policy().get("source") == "startup"))
                    desired = dict(capture.limits, binary_paths=(GROK,))
                    write_policy(root / "state" / POLICY_FILE, policy_document(desired))
                    self.assertTrue(until(lambda: policy().get("revision") == policy_revision(desired)
                                          and policy().get("source") == "reload"))
                    self.assertEqual(capture.limits["binary_paths"], (GROK,))
                finally:
                    capture.done()

    def test_restarted_addon_keeps_reloaded_policy_until_next_on(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "state").mkdir()
            (root / "data").mkdir()
            write_policy(root / "state" / POLICY_FILE,
                         policy_document(dict(capture_limits(), binary_paths=(GROK,))))
            capture = Capture(SimpleNamespace(submit=lambda r: None, policy_status=None),
                              limits=capture_limits())
            watcher = PolicyWatcher(capture, root / "state", root / "data")
            with patch("builtins.print"):
                watcher.start()
            watcher.stop()
            self.assertEqual(capture.limits["binary_paths"], (GROK,))
            # `on` writes a fresh plist and makes the profile authoritative again.
            profile = Profile(root, "/usr/bin/true", 19061, "explicit", "http://example.test", [])
            with patch.object(Profile, "plist", new_callable=PropertyMock,
                              return_value=root / "agent.plist"):
                MacOS().write_plist(profile)
            self.assertFalse((root / "state" / POLICY_FILE).exists())


class ReloadCommandTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.profile = Profile(self.root, "/usr/bin/true", 19062, "explicit", "http://example.test", [],
                               capture=dict(capture_limits(), binary_paths=[GROK]))
        self.profile.save()
        self.plist = patch.object(Profile, "plist", new_callable=PropertyMock,
                                  return_value=self.root / "agent.plist")
        self.plist.start()
        self.addCleanup(self.plist.stop)

    def test_stopped_profile_applies_next_on(self):
        result = reload(self.profile, SimpleNamespace(service_pid=lambda p: None))
        self.assertEqual((result["applied"], result["applies"]), (False, "next on"))
        self.assertFalse((self.root / "state" / POLICY_FILE).exists())

    def test_running_proxy_acknowledges_and_restart_only_keys_are_reported(self):
        started = dict(capture_limits(), keep_rolls=9)
        self.profile.plist.write_bytes(plistlib.dumps(
            {"EnvironmentVariables": {"TAP_CORE_CAPTURE": json.dumps(started)}}))
        (self.root / "data").mkdir(exist_ok=True)
        capture = Capture(SimpleNamespace(submit=lambda r: None, policy_status=None),
                          limits=capture_limits(started))
        watcher = PolicyWatcher(capture, self.root / "state", self.root / "data", interval=0.05)
        health = self.root / "state/capture.json"

        def heartbeat():
            while not watcher.stopping.is_set():
                if watcher.status:
                    health.write_text(json.dumps({"pid": 4242, "policy": watcher.status}))
                time.sleep(0.05)
        with patch("builtins.print"):
            watcher.start()
            beat = threading.Thread(target=heartbeat, daemon=True)
            beat.start()
            try:
                result = reload(self.profile, SimpleNamespace(service_pid=lambda p: 4242))
                again = reload(self.profile, SimpleNamespace(service_pid=lambda p: 4242))
            finally:
                watcher.stop()
                beat.join(timeout=2)
        self.assertTrue(result["applied"])
        self.assertEqual(result["binary_paths"], [GROK])
        self.assertEqual(result["restart_required"], ["keep_rolls"])
        self.assertEqual(capture.limits["binary_paths"], (GROK,))
        self.assertEqual(capture.limits["keep_rolls"], 9)  # restart-only key untouched
        self.assertTrue(again["unchanged"])

    def test_unacknowledged_reload_is_reported(self):
        with self.assertRaisesRegex(TapError, "did not acknowledge"):
            reload(self.profile, SimpleNamespace(service_pid=lambda p: 4242), wait_seconds=0.3)
        self.assertTrue((self.root / "state" / POLICY_FILE).exists())

    def test_invalid_profile_policy_is_rejected_before_handoff(self):
        raw = json.loads((self.root / "profile.json").read_text())
        raw["capture"]["binary_paths"] = [""]
        (self.root / "profile.json").write_text(json.dumps(raw))
        with self.assertRaisesRegex(TapError, "binary_paths"):
            Profile.load(self.root)
        self.assertFalse((self.root / "state" / POLICY_FILE).exists())


if __name__ == "__main__":
    unittest.main()
