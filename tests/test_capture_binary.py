"""Allowlisted binary (protobuf/Connect) bodies are retained as base64 (#171)."""
import base64
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tap_core.capture import (Capture, binary_retain_patterns, capture_limits,
                              wants_binary)
from tap_core.records import validate_record, RecordError

CURSOR_URL = ("https://api2.cursor.sh/aiserver.v1.DashboardService/"
              "GetCurrentPeriodUsage")
PROTO_BODY = b"\x08\x01\x12\x05hello\x18\x2a"  # arbitrary protobuf-ish bytes


def _flow(url, ctype, raw):
    response = SimpleNamespace(
        status_code=200,
        headers={"content-type": ctype},
        stream=False,
        raw_content=raw,
        get_content=lambda strict=True: raw,
        get_text=lambda **kw: raw.decode("utf-8", "replace"),
    )
    request = SimpleNamespace(method="POST", url=url, headers={}, stream=False,
                              get_text=lambda **kw: "")
    return SimpleNamespace(request=request, response=response)


def _limits(binary_paths=()):
    limits = capture_limits()
    limits["binary_paths"] = tuple(binary_paths)
    return limits


class BinaryRetentionTests(unittest.TestCase):
    def _run(self, url, ctype, raw, limits=None):
        records = []
        cap = Capture(SimpleNamespace(submit=records.append), limits=limits or _limits())
        flow = _flow(url, ctype, raw)
        cap.responseheaders(flow)
        cap.response(flow)
        self.assertEqual(len(records), 1)
        return records[0], flow

    def test_allowlisted_proto_retained_as_base64(self):
        record, flow = self._run(CURSOR_URL, "application/proto", PROTO_BODY,
                                 limits=_limits([CURSOR_URL]))
        self.assertTrue(record["body_kept"])
        self.assertEqual(record["body_reason"], "retained")
        self.assertEqual(record["body_encoding"], "base64")
        self.assertEqual(base64.b64decode(record["body"]), PROTO_BODY)
        self.assertFalse(record["streamed"])
        # header hook must NOT have forced streaming for the allowlisted path
        self.assertFalse(flow.response.stream)
        validate_record(record, allow_legacy=False)  # must not raise

    def test_no_allowlist_by_default(self):
        # The core ships vendor-free: with no binary_paths configured (and no
        # env override), a proto body is dropped on media type like any other.
        record, flow = self._run(CURSOR_URL, "application/proto", PROTO_BODY)
        self.assertFalse(record["body_kept"])
        self.assertEqual(record["body_reason"], "media_type")
        self.assertNotIn("body", record)
        self.assertNotIn("body_encoding", record)
        self.assertTrue(flow.response.stream)
        validate_record(record, allow_legacy=False)

    def test_env_override_supplies_paths(self):
        with patch.dict(os.environ,
                        {"TAP_CAPTURE_BINARY_PATHS": CURSOR_URL}):
            record, _ = self._run(CURSOR_URL, "application/proto", PROTO_BODY)
        self.assertTrue(record["body_kept"])
        self.assertEqual(record["body_encoding"], "base64")

    def test_json_path_unaffected(self):
        record, _ = self._run("https://api.example/x", "application/json", b'{"a":1}')
        self.assertTrue(record["body_kept"])
        self.assertEqual(record["body_reason"], "retained")
        self.assertNotIn("body_encoding", record)
        self.assertEqual(record["body"], '{"a":1}')

    def test_wants_binary_matches_configured_substrings(self):
        patterns = ("cursor.sh/aiserver.v1.DashboardService/",)
        self.assertTrue(wants_binary(CURSOR_URL, patterns))
        self.assertFalse(wants_binary(
            "https://api2.cursor.sh/aiserver.v1.Other/Thing", patterns))
        self.assertFalse(wants_binary(CURSOR_URL, ()))

    def test_binary_retain_patterns_env_overrides_limits(self):
        with patch.dict(os.environ, {"TAP_CAPTURE_BINARY_PATHS": "a, b ,"}):
            self.assertEqual(binary_retain_patterns({"binary_paths": ("x",)}), ("a", "b"))
        self.assertEqual(binary_retain_patterns({"binary_paths": ("x",)}), ("x",))
        self.assertEqual(binary_retain_patterns(None), ())

    def test_capture_limits_accepts_optional_binary_paths(self):
        value = {"version": 1, **{k: v for k, v in capture_limits().items()
                                  if k != "binary_paths"},
                 "binary_paths": ["cursor.sh/x/"]}
        limits = capture_limits(value)
        self.assertEqual(limits["binary_paths"], ("cursor.sh/x/",))
        # absent → empty; existing profiles keep validating
        without = {k: v for k, v in value.items() if k != "binary_paths"}
        self.assertEqual(capture_limits(without)["binary_paths"], ())

    def test_capture_limits_rejects_bad_binary_paths(self):
        base = {k: v for k, v in capture_limits().items() if k != "binary_paths"}
        for bad in (["ok", 1], [""], "not-a-list", ["x"] * 65):
            with self.assertRaises(ValueError, msg=repr(bad)):
                capture_limits({**base, "binary_paths": bad})

    def test_body_encoding_requires_retained_body(self):
        with self.assertRaises(RecordError):
            validate_record({"record_version": 1, "url": "https://x/y", "status": 200,
                             "record_id": "00000000-0000-0000-0000-000000000000",
                             "method": "GET", "ctype": "", "ua": "", "ts": 1.0, "size": 0,
                             "streamed": False, "body_kept": False, "body_reason": "media_type",
                             "req_body_kept": False, "req_body_reason": "response_not_retained",
                             "body_encoding": "base64"}, allow_legacy=False)


if __name__ == "__main__":
    unittest.main()
