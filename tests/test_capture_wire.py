"""Streamed responses and WebSocket messages teed into wire.jsonl (#250).

Also the regression that a `binary_paths` match must never buffer an
open-ended stream (SSE, Connect streaming, gRPC).
"""
import asyncio
import base64
from enum import IntEnum
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch

from tap_core.capture import (POLICY_FILE, Capture, PolicyWatcher, Wire, Writer,
                              apply_policy_document, capture_limits, policy_document,
                              policy_revision, stream_retain_patterns, streaming_media)
from tap_core.capture_policy import write_policy
from tap_core.records import (RecordError, decode_wire_part, validate_record,
                              validate_wire_part)

SSE_URL = "https://api.example.test/v1/messages?beta=true"
WATCH_URL = "https://api2.example.test/aiserver.v1.BotService/WatchTranscripts"
WS_URL = "wss://ws.example.test/socket"


def make_flow(url=SSE_URL, ctype="text/event-stream", status=200, headers=None,
              raw=None, request_body='{"stream":true}'):
    response_headers = {"content-type": ctype}
    response_headers.update(headers or {})
    response = SimpleNamespace(
        status_code=status, headers=response_headers, stream=False, raw_content=raw,
        get_content=lambda strict=True: raw,
        get_text=lambda **kw: None if raw is None else raw.decode("utf-8", "replace"))
    request = SimpleNamespace(method="POST", url=url, headers={"user-agent": "fixture/1"},
                              stream=False, timestamp_start=time.time(),
                              get_text=lambda **kw: request_body)
    return SimpleNamespace(id=str(uuid.uuid4()), metadata={}, request=request,
                           response=response, websocket=None, error=None)


class FakeWriter:
    """Records submitted parts; `refuse` drops the next N submissions."""

    def __init__(self):
        self.parts = []
        self.refuse = 0
        self.closed = False

    def submit(self, record, size=None):
        if self.refuse:
            self.refuse -= 1
            return False
        encoded = dict(record, data=base64.b64encode(record["data"]).decode("ascii"))
        self.parts.append(validate_wire_part(json.loads(json.dumps(encoded))))
        return True

    def close(self):
        self.closed = True


def limits(**overrides):
    return dict(capture_limits(), **{key: tuple(value) for key, value in overrides.items()})


def joined(parts):
    return b"".join(base64.b64decode(part["data"]) for part in parts)


class Harness(unittest.TestCase):
    def setUp(self):
        self.records = []
        self.wire_writer = FakeWriter()
        self.wire = Wire("unused", "unused", writer=self.wire_writer)
        self.capture = Capture(SimpleNamespace(submit=self.records.append),
                               limits=limits(stream_paths=["api.example.test/v1/messages",
                                                           "BotService/Watch", WS_URL]),
                               wire=self.wire)

    def stream(self, flow, chunks):
        """Drive a flow through mitmproxy's streaming calls; return client bytes."""
        self.capture.responseheaders(flow)
        stream = flow.response.stream
        delivered = []
        for chunk in list(chunks) + [b""]:
            result = stream(chunk) if callable(stream) else chunk
            delivered.extend([result] if isinstance(result, bytes) else list(result))
        self.capture.response(flow)
        return b"".join(delivered)


class BinaryPathsNeverBufferStreamsTests(Harness):
    def test_streaming_media_types(self):
        for ctype in ("text/event-stream", "text/event-stream; charset=utf-8",
                      "application/connect+proto", "application/connect+json",
                      "application/grpc", "application/grpc-web+proto", "Application/GRPC+proto"):
            self.assertTrue(streaming_media(ctype), ctype)
        for ctype in ("application/proto", "application/x-protobuf", "application/json",
                      "application/octet-stream", ""):
            self.assertFalse(streaming_media(ctype), ctype)

    def test_binary_match_on_open_ended_stream_still_streams(self):
        # Regression: no Content-Length -> size 0 used to pass the binary guard
        # and keep a server stream bufferable until it ended or hit 4 MiB.
        capture = Capture(SimpleNamespace(submit=self.records.append),
                          limits=limits(binary_paths=["BotService/", "api.example.test/"]))
        for url, ctype in ((WATCH_URL, "application/connect+proto"),
                           (SSE_URL, "text/event-stream"),
                           (WATCH_URL, "application/grpc")):
            with self.subTest(ctype=ctype):
                flow = make_flow(url, ctype)
                capture.responseheaders(flow)
                self.assertIs(flow.response.stream, True)
                capture.response(flow)
                record = self.records[-1]
                self.assertEqual((record["streamed"], record["body_reason"]), (True, "media_type"))
                self.assertNotIn("body_encoding", record)
                validate_record(record, allow_legacy=False)

    def test_unary_binary_without_content_length_stays_retained(self):
        # Unary HTTP/2 bodies often omit Content-Length; #171 must keep working.
        capture = Capture(SimpleNamespace(submit=self.records.append),
                          limits=limits(binary_paths=["BotService/"]))
        flow = make_flow(WATCH_URL.replace("Watch", "List"), "application/proto", raw=b"\x08\x01")
        capture.responseheaders(flow)
        self.assertFalse(flow.response.stream)
        capture.response(flow)
        self.assertEqual(self.records[-1]["body_encoding"], "base64")

    def test_stream_paths_win_over_binary_paths_for_streams(self):
        self.capture.limits = dict(self.capture.limits, binary_paths=("BotService/",))
        flow = make_flow(WATCH_URL, "application/connect+proto")
        self.stream(flow, [b"\x00\x00\x00\x00\x02hi"])
        self.assertTrue(self.records[-1]["streamed"])
        self.assertEqual(joined(self.wire_writer.parts), b"\x00\x00\x00\x00\x02hi")


class StreamPathsPolicyTests(unittest.TestCase):
    def test_optional_validated_and_env_override(self):
        base = {k: v for k, v in capture_limits().items() if k != "stream_paths"}
        self.assertEqual(capture_limits(base)["stream_paths"], ())
        self.assertEqual(capture_limits(dict(base, stream_paths=[" a/ "]))["stream_paths"], ("a/",))
        for bad in (["ok", 1], [""], "x", ["x"] * 65, ["x" * 513]):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, "stream_paths"):
                capture_limits(dict(base, stream_paths=bad))
        with patch.dict(os.environ, {"TAP_CAPTURE_STREAM_PATHS": "a, b,"}):
            self.assertEqual(stream_retain_patterns({"stream_paths": ("x",)}), ("a", "b"))
        self.assertEqual(stream_retain_patterns(None), ())

    def test_revision_and_document_unchanged_without_stream_paths(self):
        # A policy that does not use stream_paths keeps its #247 revision and
        # document, so a proxy started before this key still accepts it.
        desired = dict(capture_limits(), binary_paths=("b/",), request_body_paths=("r/",))
        old = json.dumps({"binary_paths": ["b/"], "request_body_paths": ["r/"]},
                         sort_keys=True, separators=(",", ":"))
        self.assertEqual(policy_revision(desired), hashlib.sha256(old.encode()).hexdigest()[:16])
        self.assertNotIn("stream_paths", policy_document(desired))
        self.assertEqual(apply_policy_document(policy_document(desired), capture_limits())
                         ["stream_paths"], ())
        explicit = dict(policy_document(desired), stream_paths=[])
        self.assertEqual(apply_policy_document(explicit, capture_limits())["stream_paths"], ())

    def test_stream_paths_round_trip_through_reload_document(self):
        desired = dict(capture_limits(), stream_paths=("api.example.test/v1/messages",))
        document = policy_document(desired)
        self.assertEqual(document["stream_paths"], ["api.example.test/v1/messages"])
        self.assertNotEqual(document["revision"], policy_revision(capture_limits()))
        updated = apply_policy_document(document, capture_limits())
        self.assertEqual(updated["stream_paths"], ("api.example.test/v1/messages",))
        with self.assertRaisesRegex(ValueError, "stream_paths must be a list"):
            apply_policy_document(dict(document, stream_paths=[""]), capture_limits())


class TeeTests(Harness):
    def test_sse_bytes_reach_client_unchanged_and_are_teed_with_boundaries(self):
        chunks = [b"event: message_start\ndata: {}\n\n", b"event: message_delta\n",
                  b"data: {\"usage\":{\"output_tokens\":7}}\n\n"]
        flow = make_flow()
        delivered = self.stream(flow, chunks)
        self.assertEqual(delivered, b"".join(chunks))
        record = self.records[-1]
        validate_record(record, allow_legacy=False)
        self.assertEqual((record["streamed"], record["body_kept"], record["body_reason"]),
                         (True, False, "media_type"))
        self.assertEqual((record["req_body_kept"], record["req_body"]), (True, '{"stream":true}'))
        parts = self.wire_writer.parts
        self.assertEqual([p["flow_id"] for p in parts], [record["record_id"]] * len(parts))
        self.assertEqual(flow.metadata["tap_wire"], record["record_id"])
        self.assertEqual(joined(parts), b"".join(chunks))
        self.assertEqual([c[1] for p in parts for c in p["chunks"]], [len(c) for c in chunks])
        final = parts[-1]
        self.assertEqual((final["final"], final["outcome"], final["bytes_seen"], final["bytes_kept"]),
                         (True, "complete", sum(map(len, chunks)), sum(map(len, chunks))))
        self.assertEqual((final["transport"], final["url"], final["status"], final["ua"]),
                         ("http", SSE_URL, 200, "fixture/1"))
        self.assertFalse(self.wire.flows)

    def test_parts_flush_by_size_and_keep_numbering(self):
        self.wire.part_bytes = 10
        flow = make_flow()
        self.stream(flow, [b"0123456789", b"abc", b"defghijklm"])
        parts = self.wire_writer.parts
        self.assertEqual([p["part"] for p in parts], [0, 1, 2])
        self.assertEqual([base64.b64decode(p["data"]) for p in parts],
                         [b"0123456789", b"abcdefghijklm", b""])
        self.assertEqual([p["final"] for p in parts], [False, False, True])
        self.assertIsNone(parts[-1]["t_first"])

    def test_part_flushes_after_its_time_window_on_next_chunk(self):
        self.wire.part_seconds = 0
        flow = make_flow()
        self.stream(flow, [b"a", b"b"])
        self.assertEqual([base64.b64decode(p["data"]) for p in self.wire_writer.parts],
                         [b"a", b"b", b""])

    def test_idle_part_flushes_on_event_loop_timer(self):
        self.wire.part_seconds = 0.05
        flow = make_flow()

        async def scenario():
            self.capture.responseheaders(flow)
            flow.response.stream(b"keepalive")
            await asyncio.sleep(0.2)
            return list(self.wire_writer.parts)
        flushed = asyncio.run(scenario())
        self.assertEqual(len(flushed), 1)
        self.assertEqual(base64.b64decode(flushed[0]["data"]), b"keepalive")
        self.assertFalse(flushed[0]["final"])

    def test_flow_cap_keeps_prefix_and_boundaries_then_truncated(self):
        self.wire.flow_bytes = 5
        flow = make_flow()
        delivered = self.stream(flow, [b"abc", b"defg", b"hij"])
        self.assertEqual(delivered, b"abcdefghij")
        parts = self.wire_writer.parts
        self.assertEqual(joined(parts), b"abcde")
        self.assertEqual([c[1] for p in parts for c in p["chunks"]], [3, 4, 3])
        self.assertEqual((parts[-1]["outcome"], parts[-1]["bytes_seen"], parts[-1]["bytes_kept"]),
                         ("truncated", 10, 5))
        self.assertEqual(self.wire.counters["flows_truncated"], 1)

    def test_dropped_part_is_counted_on_next_part(self):
        self.wire.part_bytes = 1
        flow = make_flow()
        self.capture.responseheaders(flow)
        flow.response.stream(b"a")
        self.wire_writer.refuse = 1
        flow.response.stream(b"b")
        flow.response.stream(b"c")
        flow.response.stream(b"")
        parts = self.wire_writer.parts
        self.assertEqual([p["part"] for p in parts], [0, 2, 3])
        self.assertEqual([p["parts_lost_before"] for p in parts], [0, 1, 0])
        self.assertEqual(self.wire.counters["parts_dropped"], 1)

    def test_tracked_flow_bound_refuses_explicitly(self):
        self.wire.max_flows = 1
        first, second = make_flow(), make_flow()
        self.capture.responseheaders(first)
        self.capture.responseheaders(second)
        self.assertTrue(callable(first.response.stream))
        self.assertIs(second.response.stream, True)
        refused = self.wire_writer.parts[-1]
        self.assertEqual((refused["final"], refused["outcome"], refused["data"]), (True, "refused", ""))
        self.capture.response(second)
        self.assertNotEqual(self.records[-1]["record_id"], refused["flow_id"])
        self.assertEqual(self.wire.counters["flows_refused"], 1)

    def test_error_and_shutdown_close_open_flows(self):
        aborted, open_flow = make_flow(), make_flow()
        self.capture.responseheaders(aborted)
        aborted.response.stream(b"partial")
        aborted.error = "Client disconnected."
        self.capture.error(aborted)
        self.capture.responseheaders(open_flow)
        open_flow.response.stream(b"still open")
        self.capture.writer = SimpleNamespace(close=lambda: None)
        self.capture.done()
        by_flow = {}
        for part in self.wire_writer.parts:
            by_flow.setdefault(part["flow_id"], []).append(part)
        outcomes = sorted((parts[-1]["outcome"], joined(parts)) for parts in by_flow.values())
        self.assertEqual(outcomes, [("error", b"partial"), ("shutdown", b"still open")])
        self.assertTrue(self.wire_writer.closed)
        self.assertEqual(self.wire.counters["flows_error"], 1)

    def test_composes_with_existing_transform(self):
        flow = make_flow()
        flow.response.stream = lambda chunk: chunk.upper()
        self.capture.responseheaders(flow)
        self.assertEqual(flow.response.stream(b"abc"), b"ABC")
        flow.response.stream(b"")
        self.assertEqual(joined(self.wire_writer.parts), b"ABC")

        iterable = make_flow()
        iterable.response.stream = lambda chunk: [chunk[:1], chunk[1:]] if chunk else []
        self.capture.responseheaders(iterable)
        self.assertEqual(list(iterable.response.stream(b"xyz")), [b"x", b"yz"])
        list(iterable.response.stream(b""))
        parts = [p for p in self.wire_writer.parts if p["flow_id"] == iterable.metadata["tap_wire"]]
        self.assertEqual([c[1] for p in parts for c in p["chunks"]], [1, 2])
        self.assertEqual(parts[-1]["outcome"], "complete")

    def test_unmatched_and_retained_responses_are_untouched(self):
        other = make_flow("https://elsewhere.test/stream")
        self.capture.responseheaders(other)
        self.assertIs(other.response.stream, True)
        json_flow = make_flow(SSE_URL, "application/json", raw=b'{"a":1}')
        self.capture.responseheaders(json_flow)
        self.assertFalse(json_flow.response.stream)
        self.capture.response(json_flow)
        self.assertEqual(self.records[-1]["body"], '{"a":1}')
        self.assertEqual(self.wire_writer.parts, [])

    def test_empty_streamed_body_closes_in_response_hook(self):
        flow = make_flow()
        self.capture.responseheaders(flow)
        self.capture.response(flow)  # end_stream at headers: tee never called
        self.assertEqual(self.wire_writer.parts[-1]["outcome"], "complete")
        self.assertFalse(self.wire.flows)

    def test_reload_never_changes_an_open_flow(self):
        flow = make_flow()
        self.capture.responseheaders(flow)
        self.capture.limits = capture_limits()  # stream_paths removed by a reload
        flow.response.stream(b"data: 1\n\n")
        flow.response.stream(b"")
        self.capture.response(flow)
        self.assertEqual(joined(self.wire_writer.parts), b"data: 1\n\n")
        self.assertEqual(self.records[-1]["record_id"], self.wire_writer.parts[0]["flow_id"])
        self.assertTrue(self.records[-1]["req_body_kept"])
        later = make_flow()
        self.capture.responseheaders(later)
        self.assertIs(later.response.stream, True)


class WebSocketTests(Harness):
    def message(self, flow, content, from_client, opcode):
        flow.websocket.messages.append(SimpleNamespace(content=content, from_client=from_client,
                                                       type=opcode))
        self.capture.websocket_message(flow)

    def test_both_directions_text_and_binary_with_exact_boundaries(self):
        flow = make_flow(WS_URL, "", status=101, request_body="")
        self.capture.responseheaders(flow)
        self.assertIs(flow.response.stream, True)
        flow.websocket = SimpleNamespace(messages=[], close_code=None)
        self.capture.response(flow)
        handshake = self.records[-1]
        self.assertEqual(handshake["status"], 101)
        validate_record(handshake, allow_legacy=False)
        self.assertEqual(self.wire_writer.parts, [])  # still open after the handshake
        self.message(flow, b'{"op":"hello"}', True, 1)
        self.message(flow, b"\x00\x01\x02", False, 2)
        self.message(flow, b"pong", False, 1)
        flow.websocket.close_code = IntEnum("CloseReason", {"NORMAL_CLOSURE": 1000}).NORMAL_CLOSURE
        self.capture.websocket_end(flow)
        parts = self.wire_writer.parts
        self.assertEqual({p["flow_id"] for p in parts}, {handshake["record_id"]})
        chunks = [c for p in parts for c in p["chunks"]]
        self.assertEqual([(c[1], c[2], c[3]) for c in chunks],
                         [(14, "c", 1), (3, "s", 2), (4, "s", 1)])
        self.assertEqual(joined(parts), b'{"op":"hello"}\x00\x01\x02pong')
        self.assertEqual((parts[-1]["transport"], parts[-1]["outcome"], parts[-1]["close_code"]),
                         ("websocket", "complete", 1000))

    def test_101_without_websocket_layer_closes_immediately(self):
        flow = make_flow(WS_URL, "", status=101)
        self.capture.responseheaders(flow)
        self.capture.response(flow)
        self.assertEqual(self.wire_writer.parts[-1]["outcome"], "complete")
        self.assertFalse(self.wire.flows)


class WireValidatorTests(unittest.TestCase):
    def part(self, **overrides):
        record = {"schema": "tap.capture.wire/v1", "record_id": str(uuid.uuid4()),
                  "flow_id": str(uuid.uuid4()), "part": 0, "final": True, "transport": "http",
                  "method": "POST", "url": SSE_URL, "status": 200, "ctype": "text/event-stream",
                  "content_encoding": "", "ua": "", "t_request": 1.0, "t_headers": 2.0,
                  "t_first": 2.5, "t_last": 3.0, "chunks": [[500, 3]],
                  "data": base64.b64encode(b"abc").decode(), "bytes_seen": 3, "bytes_kept": 3,
                  "parts_lost_before": 0, "outcome": "complete"}
        record.update(overrides)
        return record

    def test_valid_and_invalid_parts(self):
        validate_wire_part(self.part())
        cases = {
            "schema": dict(schema="tap.capture.wire/v2"),
            "flow_id": dict(flow_id="nope"),
            "data must be base64": dict(data="***"),
            "more bytes than its chunks": dict(chunks=[[0, 1]]),
            "outcome": dict(final=False),
            "outcome ": dict(outcome="lost"),
            "chunk entry": dict(chunks=[[0, 3, "c", 1]]),
            "no timestamps": dict(chunks=[], data=""),
            "more bytes than it saw": dict(bytes_kept=4),
        }
        for message, overrides in cases.items():
            with self.subTest(message=message), self.assertRaises(RecordError):
                validate_wire_part(self.part(**overrides))
        validate_wire_part(self.part(transport="websocket", chunks=[[0, 3, "s", 2]], close_code=1006))
        with self.assertRaises(RecordError):
            decode_wire_part('{"schema": "tap.capture.wire/v1", "schema": "x"}')


class WireJournalTests(unittest.TestCase):
    def test_real_writer_appends_valid_lines_and_health(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wire = Wire(root, root, part_bytes=4)
            capture = Capture(SimpleNamespace(submit=lambda record: None),
                              limits=limits(stream_paths=["api.example.test/"]), wire=wire)
            flow = make_flow()
            capture.responseheaders(flow)
            for chunk in (b"\x00\xff" * 3, b"tail", b""):
                flow.response.stream(chunk)
            capture.response(flow)
            wire.close()
            lines = (root / "wire.jsonl").read_bytes().splitlines()
            parts = [decode_wire_part(line) for line in lines]
            self.assertEqual(joined(parts), b"\x00\xff" * 3 + b"tail")
            self.assertEqual(parts[-1]["outcome"], "complete")
            health = json.loads((root / "wire.json").read_text())
            self.assertEqual((health["schema"], health["written"], health["flows_started"],
                              health["parts_dropped"], health["flows_open"]),
                             ("tap.capture.wire/v1", len(parts), 1, 0, 0))
            self.assertFalse((root / "stream.jsonl").exists())

    def test_profile_without_stream_paths_creates_no_wire_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "data").mkdir()
            (root / "state").mkdir()
            env = {"TAP_CORE_DATA": str(root / "data"), "TAP_CORE_STATE": str(root / "state"),
                   "TAP_CORE_CAPTURE": json.dumps(capture_limits())}
            with patch.dict(os.environ, env), patch("builtins.print"):
                capture = Capture()
                capture.load(None)
                try:
                    flow = make_flow()
                    capture.responseheaders(flow)
                    self.assertIs(flow.response.stream, True)
                    capture.response(flow)
                finally:
                    capture.done()
            self.assertFalse((root / "data/wire.jsonl").exists())
            self.assertFalse((root / "state/wire.json").exists())

    def test_wire_writer_rolls_its_own_segments(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            writer = Writer(root, root, limits=dict(capture_limits(), segment_bytes=1),
                            journal="wire.jsonl", health="wire.json",
                            encode=lambda record: (json.dumps(record) + "\n").encode())
            for index in range(3):
                self.assertTrue(writer.submit({"i": index}, size=10))
            writer.close()
            self.assertEqual(len(writer.archives()), 2)
            self.assertTrue(all(p.name.startswith("wire.jsonl.") for p in writer.archives()))
            self.assertFalse((root / "stream.jsonl").exists())


class StreamPathsReloadTests(unittest.TestCase):
    def test_reload_adds_and_removes_stream_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            state, data = Path(tmp) / "state", Path(tmp) / "data"
            state.mkdir()
            data.mkdir()
            writer = FakeWriter()
            capture = Capture(SimpleNamespace(submit=lambda record: None, policy_status=None),
                              limits=capture_limits(), wire=Wire(data, state, writer=writer))
            watcher = PolicyWatcher(capture, state, data)
            with patch("builtins.print"):
                watcher.started()
                write_policy(state / POLICY_FILE, policy_document(
                    dict(capture.limits, stream_paths=("api.example.test/",))))
                self.assertTrue(watcher.check())
            self.assertEqual(capture.writer.policy_status["stream_paths"], 1)
            flow = make_flow()
            capture.responseheaders(flow)
            self.assertTrue(callable(flow.response.stream))
            with patch("builtins.print"):
                write_policy(state / POLICY_FILE, policy_document(capture_limits()))
                self.assertTrue(watcher.check())
            flow.response.stream(b"x")
            flow.response.stream(b"")
            self.assertEqual(joined(writer.parts), b"x")
            events = [json.loads(line) for line in (data / "capture-policy.jsonl").read_text().splitlines()]
            self.assertEqual(events[1]["changes"]["stream_paths"],
                             {"added": ["api.example.test/"], "removed": []})
            self.assertEqual(events[2]["changes"]["stream_paths"],
                             {"added": [], "removed": ["api.example.test/"]})


if __name__ == "__main__":
    unittest.main()
