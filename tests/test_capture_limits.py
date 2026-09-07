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
                              fit_record, mitm_size, DEFAULT_CAPTURE)
from tap_core.journal import Journal
from tap_core.records import MAX_RECORD_BYTES, decode_record, validate_record
from tap_core.runtime import MacOS, Profile, TapError


class CaptureLimitsTests(unittest.TestCase):
    def test_defaults_match_previous_hardcoded_bounds(self):
        limits = capture_limits()
        self.assertEqual(limits["stream_large_bodies"], 4 * 1024 * 1024)
        self.assertEqual(limits["segment_bytes"], 128 * 1024 * 1024)
        self.assertEqual(limits["keep_rolls"], 3)
        self.assertEqual(limits["queue_slots"], 64)
        self.assertEqual(limits["queue_bytes"], 16 * 1024 * 1024)
        self.assertEqual(limits["max_body_bytes"], 12 * 1024 * 1024)
        self.assertEqual(mitm_size(limits["stream_large_bodies"]), "4m")
        self.assertLessEqual(2 * limits["max_body_bytes"] + 1024 * 1024, MAX_RECORD_BYTES)

    def test_invalid_limits_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "positive int"):
            capture_limits({**DEFAULT_CAPTURE, "queue_slots": 0})
        with self.assertRaisesRegex(ValueError, "must not exceed queue_bytes"):
            capture_limits({**DEFAULT_CAPTURE, "max_body_bytes": 13 * 1024 * 1024,
                            "queue_bytes": 12 * 1024 * 1024})
        with self.assertRaisesRegex(ValueError, "supported maximum \\(12 MiB\\)"):
            capture_limits({**DEFAULT_CAPTURE, "max_body_bytes": 13 * 1024 * 1024,
                            "queue_bytes": 64 * 1024 * 1024})

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

    def test_decide_body_allows_chunked_until_raw_or_backend_stream(self):
        response = Mock()
        response.headers = {"content-type": "application/json"}
        response.stream = False
        response.raw_content = None
        keep, reason, force_stream, size = decide_body(response, max_body_bytes=50)
        self.assertTrue(keep)
        self.assertEqual(reason, "retained")
        self.assertFalse(force_stream)
        self.assertEqual(size, 0)
        response.raw_content = b"x" * 200
        keep, reason, force_stream, size = decide_body(response, max_body_bytes=50)
        self.assertFalse(keep)
        self.assertEqual(reason, "oversize")
        self.assertEqual(size, 200)

    def test_chunked_json_survives_responseheaders_to_response(self):
        records = []
        limits = {**DEFAULT_CAPTURE, "max_body_bytes": 1024}
        capture = Capture(SimpleNamespace(submit=records.append, limits=limits), limits=limits)
        response = SimpleNamespace(status_code=200,
                                   headers={"content-type": "application/json"},
                                   stream=False, raw_content=None,
                                   get_text=lambda **kw: '{"ok":true}')
        request = SimpleNamespace(method="GET", url="https://fixture.example/chunked",
                                  headers={}, stream=False, get_text=lambda **kw: "")
        flow = SimpleNamespace(request=request, response=response)
        capture.responseheaders(flow)
        self.assertFalse(response.stream)
        response.raw_content = b'{"ok":true}'
        capture.response(flow)
        self.assertTrue(records[0]["body_kept"])
        self.assertEqual(records[0]["body"], '{"ok":true}')
        validate_record(records[0])

    def test_header_oversize_preserves_reason_and_size_after_forced_stream(self):
        records = []
        limits = {**DEFAULT_CAPTURE, "max_body_bytes": 128}
        capture = Capture(SimpleNamespace(submit=records.append, limits=limits), limits=limits)
        response = SimpleNamespace(status_code=200,
                                   headers={"content-type": "application/json",
                                            "content-length": "202"},
                                   stream=False, raw_content=None,
                                   get_text=lambda **kw: (_ for _ in ()).throw(AssertionError("no decode")))
        request = SimpleNamespace(method="GET", url="https://fixture.example/big",
                                  headers={}, stream=False, get_text=lambda **kw: "")
        flow = SimpleNamespace(request=request, response=response)
        capture.responseheaders(flow)
        self.assertTrue(response.stream)
        capture.response(flow)
        self.assertEqual(records[0]["body_reason"], "oversize")
        self.assertEqual(records[0]["size"], 202)
        self.assertTrue(records[0]["streamed"])
        self.assertFalse(records[0]["body_kept"])
        validate_record(records[0])

    def test_media_type_preserves_declared_size(self):
        records = []
        capture = Capture(SimpleNamespace(submit=records.append))
        response = SimpleNamespace(status_code=200,
                                   headers={"content-type": "application/octet-stream",
                                            "content-length": "9000000"},
                                   stream=False, raw_content=None,
                                   get_text=lambda **kw: (_ for _ in ()).throw(AssertionError("no")))
        request = SimpleNamespace(method="GET", url="https://fixture.example/bin", headers={})
        flow = SimpleNamespace(request=request, response=response)
        capture.responseheaders(flow)
        capture.response(flow)
        self.assertEqual(records[0]["body_reason"], "media_type")
        self.assertEqual(records[0]["size"], 9000000)
        validate_record(records[0])

    def test_new_dispositions_roundtrip_writer_journal_reader(self):
        root = Path(tempfile.mkdtemp())
        limits = {**DEFAULT_CAPTURE, "max_body_bytes": 128, "queue_bytes": 10_000_000}
        writer = Writer(root, root, limits=limits)
        capture = Capture(writer=writer, limits=limits)
        for reason, headers, raw, body in (
            ("oversize", {"content-type": "application/json", "content-length": "500"}, None, None),
            ("oversize_decoded", {"content-type": "application/json", "content-length": "4"}, b"abcd", "x" * 400),
            ("media_type", {"content-type": "application/octet-stream", "content-length": "20"}, None, None),
        ):
            response = SimpleNamespace(status_code=200, headers=headers, stream=False,
                                       raw_content=raw, get_text=lambda body=body, **kw: body)
            request = SimpleNamespace(method="GET", url=f"https://fixture.example/{reason}",
                                      headers={}, stream=False, get_text=lambda **kw: "")
            flow = SimpleNamespace(request=request, response=response)
            capture.responseheaders(flow)
            capture.response(flow)
        writer.close(timeout=2)
        entries = list(Journal(root).scan())
        reasons = {entry.record["url"].rsplit("/", 1)[-1]: entry.record["body_reason"] for entry in entries}
        self.assertEqual(reasons["oversize"], "oversize")
        self.assertEqual(reasons["oversize_decoded"], "oversize_decoded")
        self.assertEqual(reasons["media_type"], "media_type")
        for entry in entries:
            validate_record(entry.record)
            decode_record((json.dumps(entry.record) + "\n").encode(), allow_legacy=False)

    def test_fit_record_strips_bodies_before_journal_limit(self):
        body = "r" * (17 * 1024 * 1024)
        req = "q" * (17 * 1024 * 1024)
        record = {"record_version": 1, "record_id": "11111111-1111-4111-8111-111111111111",
                  "ts": 1.0, "method": "POST", "url": "https://fixture.example/pair",
                  "status": 200, "ctype": "application/json", "size": len(body),
                  "body_kept": True, "body_reason": "retained", "streamed": False,
                  "req_body_kept": True, "req_body_reason": "retained", "ua": "",
                  "body": body, "req_body": req}
        too_big = (json.dumps(record, ensure_ascii=False) + "\n").encode()
        self.assertGreater(len(too_big), MAX_RECORD_BYTES)
        fitted = fit_record(record)
        line = (json.dumps(fitted, ensure_ascii=False) + "\n").encode()
        self.assertLessEqual(len(line), MAX_RECORD_BYTES)
        self.assertFalse(fitted["req_body_kept"])
        self.assertEqual(fitted["req_body_reason"], "oversize_decoded")
        validate_record(fitted)
        # Writer refuses leftover oversized lines as a safety net.
        root = Path(tempfile.mkdtemp())
        writer = Writer(root, root, limits={**DEFAULT_CAPTURE, "queue_bytes": 64 * 1024 * 1024})
        writer.submit({"pad": "x" * (MAX_RECORD_BYTES + 10)})
        # Force a line that still exceeds after naive submit of raw dict:
        huge = dict(fitted)
        huge["body"] = "z" * (MAX_RECORD_BYTES)
        huge["body_kept"] = True
        huge["body_reason"] = "retained"
        writer.submit(huge)
        writer.close(timeout=2)
        health = json.loads((root / "capture.json").read_text())
        self.assertGreaterEqual(health["dropped"], 1)

    def test_decoded_body_over_budget_is_omitted(self):
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
        validate_record(records[0])

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
        capture.responseheaders(flow)
        capture.response(flow)
        writer.close(timeout=2)
        record = json.loads((root / "stream.jsonl").read_text().splitlines()[-1])
        self.assertEqual(record["body_reason"], "oversize")
        self.assertEqual(record["size"], 100)
        validate_record(record)
