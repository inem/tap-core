"""Allowlisted binary (protobuf/Connect) bodies are retained as base64 (#171)."""
import base64
import unittest
from types import SimpleNamespace

from tap_core.capture import Capture, wants_binary
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


class BinaryRetentionTests(unittest.TestCase):
    def _run(self, url, ctype, raw):
        records = []
        cap = Capture(SimpleNamespace(submit=records.append))
        flow = _flow(url, ctype, raw)
        cap.responseheaders(flow)
        cap.response(flow)
        self.assertEqual(len(records), 1)
        return records[0], flow

    def test_allowlisted_proto_retained_as_base64(self):
        record, flow = self._run(CURSOR_URL, "application/proto", PROTO_BODY)
        self.assertTrue(record["body_kept"])
        self.assertEqual(record["body_reason"], "retained")
        self.assertEqual(record["body_encoding"], "base64")
        self.assertEqual(base64.b64decode(record["body"]), PROTO_BODY)
        self.assertFalse(record["streamed"])
        # header hook must NOT have forced streaming for the allowlisted path
        self.assertFalse(flow.response.stream)
        validate_record(record, allow_legacy=False)  # must not raise

    def test_non_allowlisted_proto_still_dropped(self):
        record, flow = self._run(
            "https://other.example/aiserver.v1.Foo/Bar", "application/proto", PROTO_BODY)
        self.assertFalse(record["body_kept"])
        self.assertEqual(record["body_reason"], "media_type")
        self.assertNotIn("body", record)
        self.assertNotIn("body_encoding", record)
        self.assertTrue(flow.response.stream)  # dropped bodies still stream
        validate_record(record, allow_legacy=False)

    def test_json_path_unaffected(self):
        record, _ = self._run("https://api.example/x", "application/json", b'{"a":1}')
        self.assertTrue(record["body_kept"])
        self.assertEqual(record["body_reason"], "retained")
        self.assertNotIn("body_encoding", record)
        self.assertEqual(record["body"], '{"a":1}')

    def test_wants_binary_matches_cursor_dashboard(self):
        self.assertTrue(wants_binary(CURSOR_URL))
        self.assertFalse(wants_binary("https://api2.cursor.sh/aiserver.v1.Other/Thing"))

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
