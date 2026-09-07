"""Profile capture limits and pre-buffer body bounds (#8)."""
import json
import plistlib
import shlex
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, PropertyMock, patch

from tap_core.capture import (Capture, Writer, capture_limits, decide_body,
                              mitm_size, DEFAULT_CAPTURE)
from tap_core.journal import Journal
from tap_core.runtime import MacOS, Profile, TapError


class CaptureLimitsTests(unittest.TestCase):
    def test_defaults_match_previous_hardcoded_bounds(self):
        limits = capture_limits()
        self.assertEqual(limits["stream_large_bodies"], 4 * 1024 * 1024)
        self.assertEqual(limits["segment_bytes"], 128 * 1024 * 1024)
        self.assertEqual(limits["keep_rolls"], 3)
        self.assertEqual(limits["queue_slots"], 64)
        self.assertEqual(limits["queue_bytes"], 16 * 1024 * 1024)
        self.assertEqual(limits["max_body_bytes"], 16 * 1024 * 1024)
        self.assertEqual(mitm_size(limits["stream_large_bodies"]), "4m")

    def test_invalid_limits_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "positive int"):
            capture_limits({**DEFAULT_CAPTURE, "queue_slots": 0})
        with self.assertRaisesRegex(ValueError, "must not exceed queue_bytes"):
            capture_limits({**DEFAULT_CAPTURE, "max_body_bytes": 17 * 1024 * 1024,
                            "queue_bytes": 16 * 1024 * 1024})

    def test_profile_stores_validated_capture_limits(self):
        root = Path(tempfile.mkdtemp())
        limits = {**DEFAULT_CAPTURE, "max_body_bytes": 1024, "queue_bytes": 4096}
        profile = Profile(root, "/usr/bin/true", 19055, "explicit", "http://example.test", [],
                          capture=limits)
        self.assertEqual(profile.capture["max_body_bytes"], 1024)
        with self.assertRaisesRegex(TapError, "positive int"):
            Profile(root, "/usr/bin/true", 19056, "explicit", "http://example.test", [],
                    capture={**DEFAULT_CAPTURE, "segment_bytes": -1})

    def test_plist_applies_profile_stream_and_writer_limits(self):
        root = Path(tempfile.mkdtemp())
        limits = {**DEFAULT_CAPTURE, "stream_large_bodies": 1024 * 1024,
                  "queue_slots": 2, "max_body_bytes": 4096, "queue_bytes": 8192}
        profile = Profile(root, "/usr/bin/true", 19057, "explicit", "http://example.test", [],
                          capture=limits)
        with patch.object(Profile, "plist", new_callable=PropertyMock, return_value=root / "agent.plist"):
            MacOS().write_plist(profile)
            plist = plistlib.loads(profile.plist.read_bytes())
        argv = shlex.split(plist["ProgramArguments"][2].split("; exec ", 1)[1])
        self.assertIn("stream_large_bodies=1m", argv)
        env_limits = json.loads(plist["EnvironmentVariables"]["TAP_CORE_CAPTURE"])
        self.assertEqual(env_limits["queue_slots"], 2)
        self.assertEqual(env_limits["stream_large_bodies"], 1024 * 1024)

    def test_writer_honours_queue_slot_override(self):
        root = Path(tempfile.mkdtemp())
        limits = {**DEFAULT_CAPTURE, "queue_slots": 1, "queue_bytes": 10_000_000,
                  "max_body_bytes": 1024}
        writer = Writer(root, root, limits=limits)
        self.addCleanup(writer.close)
        writer.submit({"n": 1, "pad": "x" * 100})
        writer.submit({"n": 2, "pad": "x" * 100})
        writer.close(timeout=2)
        health = json.loads((root / "capture.json").read_text())
        self.assertGreaterEqual(health["dropped"], 1)

    def test_queue_drop_is_health_counter_not_journal_gap(self):
        root = Path(tempfile.mkdtemp())
        # Byte budget rejects before write; Journal only sees what was written.
        limits = {**DEFAULT_CAPTURE, "queue_slots": 64, "queue_bytes": 64 * 1024,
                  "max_body_bytes": 1024}
        writer = Writer(root, root, limits=limits)
        capture = Capture(writer=writer, limits=limits)
        kept = SimpleNamespace(status_code=200,
                               headers={"content-type": "application/json", "content-length": "2"},
                               stream=False, raw_content=b"{}", get_text=lambda **kw: "{}")
        capture.response(SimpleNamespace(
            request=SimpleNamespace(method="GET", url="https://fixture.example/ok", headers={},
                                    stream=False, get_text=lambda **kw: ""),
            response=kept))
        # Oversized submit estimate trips queue_bytes without a journal entry.
        writer.submit({"pad": "x" * (512 * 1024)})
        writer.close(timeout=2)
        health = json.loads((root / "capture.json").read_text())
        self.assertGreaterEqual(health["dropped"], 1)
        self.assertEqual(health["written"], 1)
        entries = list(Journal(root).scan())
        self.assertEqual(len(entries), 1)
        self.assertTrue(entries[0].record["url"].endswith("/ok"))

    def test_decide_body_omits_before_decode_on_content_length(self):
        response = Mock()
        response.headers = {"content-type": "application/json", "content-length": str(100)}
        response.stream = False
        response.raw_content = None
        keep, reason, force_stream, size = decide_body(response, max_body_bytes=50)
        self.assertFalse(keep)
        self.assertEqual(reason, "oversize")
        self.assertTrue(force_stream)
        self.assertEqual(size, 100)

    def test_decide_body_omits_raw_content_oversize_and_unknown_length(self):
        response = Mock()
        response.headers = {"content-type": "text/plain"}
        response.stream = False
        response.raw_content = b"x" * 200
        keep, reason, force_stream, size = decide_body(response, max_body_bytes=50)
        self.assertFalse(keep)
        self.assertEqual(reason, "oversize")
        self.assertEqual(size, 200)

        response.raw_content = None
        keep, reason, force_stream, size = decide_body(response, max_body_bytes=50)
        self.assertFalse(keep)
        self.assertEqual(reason, "unbounded")
        self.assertTrue(force_stream)

    def test_decoded_body_over_budget_is_omitted(self):
        """Compressed/wire size can be small while get_text expands past max_body_bytes."""
        records = []
        limits = {**DEFAULT_CAPTURE, "max_body_bytes": 8}
        capture = Capture(SimpleNamespace(submit=records.append, limits=limits), limits=limits)
        response = SimpleNamespace(status_code=200,
                                   headers={"content-type": "application/json", "content-length": "4"},
                                   stream=False, raw_content=b"abcd",
                                   get_text=lambda **kw: "x" * 40)
        request = SimpleNamespace(method="GET", url="https://fixture.example/z", headers={},
                                  stream=False, get_text=lambda **kw: "")
        capture.response(SimpleNamespace(request=request, response=response))
        self.assertFalse(records[0]["body_kept"])
        self.assertEqual(records[0]["body_reason"], "oversize_decoded")
        self.assertNotIn("body", records[0])

    def test_capture_records_oversize_without_body(self):
        root = Path(tempfile.mkdtemp())
        limits = {**DEFAULT_CAPTURE, "max_body_bytes": 8, "queue_bytes": 10_000_000}
        writer = Writer(root, root, limits=limits)
        self.addCleanup(writer.close)
        capture = Capture(writer=writer, limits=limits)
        flow = Mock()
        flow.request.method = "GET"
        flow.request.url = "https://fixture.example/x"
        flow.request.headers = {"user-agent": "t"}
        flow.request.stream = False
        flow.request.get_text = Mock(return_value=None)
        flow.response.headers = {"content-type": "application/json", "content-length": "100"}
        flow.response.status_code = 200
        flow.response.stream = False
        flow.response.raw_content = None
        flow.response.get_text = Mock(side_effect=AssertionError("must not decode oversize body"))
        capture.response(flow)
        writer.close(timeout=2)
        lines = (root / "stream.jsonl").read_text().splitlines()
        record = json.loads(lines[-1])
        self.assertFalse(record["body_kept"])
        self.assertEqual(record["body_reason"], "oversize")
        self.assertNotIn("body", record)
