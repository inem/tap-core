#!/usr/bin/env python3
"""Real macOS hot-reader acceptance: live HTTP -> TAP proxy -> reader output.

Runs exclusively in a new temporary profile with explicit loopback routing.
Never changes the system proxy or touches an installed production profile.
Starts a real capture proxy and Hub; checks their PIDs across reader changes.
Then reloads capture.binary_paths with `tap capture reload` (#247) and checks
that a binary body is retained only after the reload, with the same PIDs.
Finally (#250) checks that a binary_paths match never buffers an SSE or Connect
server stream (the client gets the first event while upstream is still open),
then reloads capture.stream_paths and checks that the same streams and a
WebSocket exchange are teed into data/wire.jsonl byte for byte.
"""
import argparse
import base64
import hashlib
import http.client
import os
import struct
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import platform
import socket
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tap_core.components import Job, hub_health
from tap_core.pack_store import build_artifact
from tap_core.runtime import Lifecycle, MacOS, Profile

NEIGHBOR = "fixture.hot-neighbor"
TARGET = "fixture.hot-target"


PROTO_BODY = b"\x08\x01\x12\x05hello"
STREAM_FIRST = b"event: first\ndata: {\"n\":1}\n\n"
STREAM_SECOND = b"event: second\ndata: {\"n\":2}\n\n"
STREAM_TYPES = {"sse": "text/event-stream", "connect": "application/connect+proto"}
# The origin holds a stream open after its first event until the client says it
# has read that event (or HOLD_SECONDS pass). A buffering proxy shows up as a
# client that cannot read the first event before the hold ends.
RELEASES = {}
HOLD_SECONDS = 10
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def ws_send(stream, opcode, payload, mask=False):
    head = bytes([0x80 | opcode])
    length = len(payload)
    flag = 0x80 if mask else 0
    if length < 126:
        head += bytes([flag | length])
    else:
        head += bytes([flag | 126]) + struct.pack("!H", length)
    if mask:
        key = os.urandom(4)
        payload = key + bytes(b ^ key[i % 4] for i, b in enumerate(payload))
    stream.write(head + payload) if hasattr(stream, "write") else stream.sendall(head + payload)


def ws_read(read):
    first, second = read(2)
    length = second & 0x7F
    if length == 126:
        length = struct.unpack("!H", read(2))[0]
    elif length == 127:
        length = struct.unpack("!Q", read(8))[0]
    key = read(4) if second & 0x80 else None
    payload = read(length)
    if key:
        payload = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
    return first & 0x0F, payload


class Origin(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def chunk(self, data):
        self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
        self.wfile.flush()

    def do_GET(self):
        parts = self.path.strip("/").split("/")
        if parts[0] in STREAM_TYPES:
            release = RELEASES.setdefault(self.path, threading.Event())
            self.send_response(200)
            self.send_header("Content-Type", STREAM_TYPES[parts[0]])
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self.chunk(STREAM_FIRST)
            release.wait(HOLD_SECONDS)
            self.chunk(STREAM_SECOND)
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
            return
        if parts[0] == "ws":
            key = self.headers["Sec-WebSocket-Key"]
            accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
            self.send_response(101)
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.send_header("Sec-WebSocket-Accept", accept)
            self.end_headers()
            self.wfile.flush()
            _opcode, payload = ws_read(self.rfile.read)
            ws_send(self.wfile, 0x1, b"echo:" + payload)
            ws_send(self.wfile, 0x2, b"\x00\x01\x02")
            ws_send(self.wfile, 0x8, struct.pack("!H", 1000))
            self.wfile.flush()
            self.close_connection = True
            return
        if self.path.startswith("/proto/"):
            self.send_response(200)
            self.send_header("Content-Type", "application/x-protobuf")
            self.send_header("Content-Length", str(len(PROTO_BODY)))
            self.end_headers()
            self.wfile.write(PROTO_BODY)
            return
        phase = urlsplit(self.path).path.strip("/")
        body = json.dumps({"phase": phase}, separators=(",", ":")).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


def run(arguments, *, timeout=70):
    result = subprocess.run([str(a) for a in arguments], capture_output=True,
                            text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError("Command failed: " + " ".join(map(str, arguments))
                           + "\n" + (result.stderr or result.stdout))
    return result.stdout


def wait(check, label, seconds=15):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.1)
    raise RuntimeError("Timed out: " + label)


def reserve_port():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    return listener


def reader_source(directory, name, origin):
    source = directory / name
    source.mkdir()
    (source / "reader.py").write_text(
        "import json, os, pathlib, sys\n"
        "context = json.loads(os.environ['TAP_PACK_CONTEXT'])\n"
        "target = pathlib.Path(context['output_dir']) / 'events.jsonl'\n"
        "for line in sys.stdin:\n"
        "    record = json.loads(line)\n"
        "    if not isinstance(record.get('body'), str):\n"
        "        continue\n"
        "    try:\n"
        "        phase = json.loads(record['body']).get('phase')\n"
        "    except ValueError:\n"
        "        continue\n"
        "    if not isinstance(phase, str):\n"
        "        continue\n"
        "    with target.open('a', encoding='utf-8') as out:\n"
        "        out.write(json.dumps({'phase': phase, 'record_id': record.get('record_id')}) + '\\n')\n"
    )
    (source / "pack.json").write_text(json.dumps({
        "manifest_version": 1,
        "id": name,
        "version": "0.1.0",
        "requires": {"pack_api": 1, "dependencies": []},
        "files": ["reader.py"],
        "entrypoints": {"reader": {"file": "reader.py", "interface": "python-jsonl-v1"}},
        "config": {},
        "access": {"origins": [origin], "capabilities": ["capture.read"]},
    }))
    artifact = directory / (name + ".tap-pack")
    build_artifact(source, artifact)
    return artifact


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", type=Path, required=True,
                        help="Absolute path to mitmdump 12.2.3")
    parser.add_argument("--bun", type=Path, required=True,
                        help="Absolute path to Bun 1.3.11")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if platform.system() != "Darwin":
        raise SystemExit("This acceptance check requires macOS/launchd")
    backend = args.backend.resolve(strict=True)
    bun = args.bun.resolve(strict=True)
    if "12.2.3" not in run([backend, "--version"]):
        raise RuntimeError("Backend must be mitmdump 12.2.3")
    if run([bun, "--version"]).strip() != "1.3.11":
        raise RuntimeError("Bun must be version 1.3.11")

    adapter = MacOS()
    network_before = adapter.network_state()
    report = {
        "scope": "temporary macOS launchd profile, real loopback HTTP and mitmdump",
        "core_revision": run(["git", "-C", ROOT, "rev-parse", "HEAD"]).strip(),
        "scenario_passed": False,
        "cleanup_verified": False,
        "system_proxy_unchanged": False,
    }

    origin_server = ThreadingHTTPServer(("127.0.0.1", 0), Origin)
    threading.Thread(target=origin_server.serve_forever, daemon=True).start()
    origin = "http://127.0.0.1:" + str(origin_server.server_port)
    report["origin"] = origin

    with tempfile.TemporaryDirectory(prefix="tap-live-reader-") as temporary:
        root = Path(temporary)
        profile_root = root / "profile"
        profile = None
        try:
            with reserve_port() as proxy, reserve_port() as hub:
                proxy_port, hub_port = proxy.getsockname()[1], hub.getsockname()[1]
                bridge = {"version": 1, "enabled": True, "hub_port": hub_port,
                          "allow_origins": [], "exclude_origins": [], "page_scripts": []}
                components = {"version": 1, "python": sys.executable, "bun": str(bun),
                              "readers": {}, "handlers": {}}
                (root / "bridge.json").write_text(json.dumps(bridge))
                (root / "components.json").write_text(json.dumps(components))
                proxy.close()
                hub.close()

            prefix = [sys.executable, str(ROOT / "tap"), "--profile", str(profile_root)]
            run(prefix + ["install", "--backend", backend, "--port", proxy_port,
                          "--routing", "explicit", "--probe-url", origin + "/bootstrap",
                          "--bridge-config", root / "bridge.json",
                          "--components-config", root / "components.json"])
            profile = Profile.load(profile_root)
            run(prefix + ["on"])

            def pids():
                return {
                    "capture": adapter.service_pid(profile),
                    "controller": adapter.service_pid(Job(profile)),
                    "hub": hub_health(profile)["pid"],
                }

            baseline = wait(lambda: (observed if all(observed.values()) else None)
                            if (observed := pids()) else None, "all three processes")
            report["pids"] = baseline

            def check_pids():
                actual = pids()
                assert actual == baseline, "TAP restarted during hot-plug: " + repr((baseline, actual))

            def state():
                try:
                    return json.loads((profile_root / "state/components.json").read_text())
                except (OSError, ValueError):
                    return {}

            def phases(name):
                path = profile_root / "data" / "readers" / name / "events.jsonl"
                if not path.exists():
                    return []
                return [json.loads(line)["phase"] for line in path.read_text().splitlines()
                        if line.strip()]

            def ready(name, attached=True):
                readers = state().get("readers", {})
                return ((name in readers and readers[name].get("healthy") is True)
                        if attached else name not in readers)

            def enable(name):
                run(prefix + ["pack", "enable", name, "--version", "0.1.0",
                              "--grant-origin", origin, "--grant-capability", "capture.read"])
                wait(lambda: ready(name), name + " attached")
                check_pids()

            def disable(name):
                run(prefix + ["pack", "disable", name])
                wait(lambda: ready(name, attached=False), name + " detached")
                check_pids()

            def request(phase):
                run(["/usr/bin/curl", "--fail", "--silent", "--show-error",
                     "--noproxy", "", "--proxy", "http://127.0.0.1:" + str(proxy_port),
                     origin + "/" + phase], timeout=20)

            for name in (NEIGHBOR, TARGET):
                artifact = reader_source(root, name, origin)
                run(prefix + ["pack", "install", artifact])

            enable(NEIGHBOR)
            request("before")
            wait(lambda: phases(NEIGHBOR) == ["before"], "neighbor before attachment")
            assert phases(TARGET) == []
            check_pids()

            enable(TARGET)
            request("attached")
            wait(lambda: phases(TARGET) == ["attached"], "target handles live HTTP")
            wait(lambda: phases(NEIGHBOR) == ["before", "attached"], "neighbor continues")
            check_pids()

            disable(TARGET)
            request("detached")
            wait(lambda: phases(NEIGHBOR) == ["before", "attached", "detached"],
                 "neighbor continues while target is disabled")
            assert phases(TARGET) == ["attached"], phases(TARGET)
            check_pids()

            enable(TARGET)
            request("reattached")
            wait(lambda: phases(TARGET) == ["attached", "reattached"],
                 "target resumes only from current tail")
            wait(lambda: phases(NEIGHBOR) == ["before", "attached", "detached", "reattached"],
                 "neighbor never interrupted")
            check_pids()

            captured = (profile_root / "data/stream.jsonl").read_text().splitlines()
            captured_phases = []
            for line in captured:
                record = json.loads(line)
                if not record.get("url", "").startswith(origin + "/"):
                    continue
                try:
                    captured_phases.append(json.loads(record.get("body") or "{}").get("phase"))
                except ValueError:
                    pass
            assert set(("before", "attached", "detached", "reattached")).issubset(
                captured_phases), captured_phases
            assert not state().get("error"), state().get("error")

            # Live capture retention reload (#247): same proxy, new allowlist.
            def proto_record(name):
                for line in (profile_root / "data/stream.jsonl").read_text().splitlines():
                    record = json.loads(line)
                    if record.get("url") == origin + "/proto/" + name:
                        return record
                return None

            request("proto/before-reload")
            before_reload = wait(lambda: proto_record("before-reload"), "proto before reload")
            assert before_reload["body_reason"] == "media_type", before_reload
            from tap_core.capture import capture_limits
            edited = Profile.load(profile_root)
            # The SSE/Connect prefixes are the #250 regression: a binary_paths
            # match must not make the proxy buffer an open-ended stream.
            edited.capture = dict(capture_limits(edited.capture),
                                  binary_paths=[origin + "/proto/", origin + "/sse/",
                                                origin + "/connect/"])
            edited.save()
            reloaded = json.loads(run(prefix + ["capture", "reload"]))
            assert reloaded["applied"] and reloaded["applies"] == "live", reloaded
            request("proto/after-reload")
            after_reload = wait(lambda: proto_record("after-reload"), "proto after reload")
            assert after_reload.get("body_encoding") == "base64" and after_reload["body_kept"], after_reload
            events = [json.loads(line) for line in
                      (profile_root / "data/capture-policy.jsonl").read_text().splitlines()]
            assert [e["source"] for e in events if e["event"] == "capture.policy.applied"][-1] == "reload"
            check_pids()
            report["capture_policy_reload"] = {"revision": reloaded["revision"],
                                               "before": before_reload["body_reason"],
                                               "after": after_reload["body_encoding"]}

            # Streams (#250): delivered unbuffered under binary_paths, then teed.
            def record_for(path):
                for line in (profile_root / "data/stream.jsonl").read_text().splitlines():
                    record = json.loads(line)
                    if record.get("url") == origin + path:
                        return record
                return None

            def open_stream(path):
                """Client through the proxy; seconds until the first event while upstream holds."""
                connection = http.client.HTTPConnection("127.0.0.1", proxy_port, timeout=HOLD_SECONDS / 2)
                started = time.monotonic()
                try:
                    connection.request("GET", origin + path)
                    received = b""
                    try:
                        # A buffering proxy sends not even the headers until upstream ends.
                        response = connection.getresponse()
                        while not received.endswith(b"\n\n"):
                            line = response.readline()
                            if not line:
                                break
                            received += line
                    except socket.timeout:
                        raise AssertionError(f"{path}: first event not delivered while upstream held "
                                             "the stream open (proxy buffered it)") from None
                    first = time.monotonic() - started
                    assert received == STREAM_FIRST, received
                    RELEASES.setdefault(path, threading.Event()).set()
                    rest = response.read()
                    assert rest == STREAM_SECOND, rest
                    return round(first, 3)
                finally:
                    RELEASES.setdefault(path, threading.Event()).set()
                    connection.close()

            streams = {}
            for kind in STREAM_TYPES:
                path = "/" + kind + "/binary-allowlisted"
                streams[kind] = {"first_event_seconds": open_stream(path)}
                record = wait(lambda: record_for(path), path + " record")
                assert record["streamed"] and record["body_reason"] == "media_type", record
            edited = Profile.load(profile_root)
            edited.capture = dict(capture_limits(edited.capture),
                                  stream_paths=[origin + "/sse/", origin + "/connect/", origin + "/ws/"])
            edited.save()
            reloaded = json.loads(run(prefix + ["capture", "reload"]))
            assert reloaded["applied"] and reloaded["stream_paths"], reloaded

            def wire_parts(flow_id):
                path = profile_root / "data/wire.jsonl"
                if not path.exists():
                    return None
                from tap_core.records import decode_wire_part
                parts = [decode_wire_part(line) for line in path.read_bytes().splitlines()]
                parts = [p for p in parts if p["flow_id"] == flow_id]
                return parts if parts and parts[-1]["final"] else None

            for kind in STREAM_TYPES:
                path = "/" + kind + "/teed"
                first = open_stream(path)
                record = wait(lambda: record_for(path), path + " record")
                parts = wait(lambda: wire_parts(record["record_id"]), path + " wire parts")
                data = b"".join(base64.b64decode(p["data"]) for p in parts)
                assert data == STREAM_FIRST + STREAM_SECOND, data
                assert [c[1] for p in parts for c in p["chunks"]] == [
                    len(STREAM_FIRST), len(STREAM_SECOND)], parts
                assert parts[-1]["outcome"] == "complete" and record["streamed"], (record, parts[-1])
                streams[kind].update(teed_first_event_seconds=first, parts=len(parts),
                                     bytes_kept=parts[-1]["bytes_kept"])

            # WebSocket through the explicit proxy: client text, server text + binary, close.
            client = socket.create_connection(("127.0.0.1", proxy_port), timeout=10)
            try:
                key = base64.b64encode(os.urandom(16)).decode()
                host = urlsplit(origin).netloc
                client.sendall((f"GET {origin}/ws/echo HTTP/1.1\r\nHost: {host}\r\n"
                                "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                                f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
                reader = client.makefile("rb")
                status = reader.readline()
                assert b" 101 " in status, status
                while reader.readline() not in (b"\r\n", b""):
                    pass
                ws_send(client, 0x1, b"hello", mask=True)
                received = [ws_read(reader.read) for _ in range(3)]
                assert received[:2] == [(1, b"echo:hello"), (2, b"\x00\x01\x02")], received
                assert received[2][0] == 0x8, received
                try:
                    ws_send(client, 0x8, struct.pack("!H", 1000), mask=True)
                except OSError:
                    pass
            finally:
                client.close()
            handshake = wait(lambda: record_for("/ws/echo"), "websocket handshake record")
            parts = wait(lambda: wire_parts(handshake["record_id"]), "websocket wire parts")
            chunks = [(c[1], c[2], c[3]) for p in parts for c in p["chunks"]]
            assert chunks == [(5, "c", 1), (10, "s", 1), (3, "s", 2)], chunks
            assert b"".join(base64.b64decode(p["data"]) for p in parts) == b"helloecho:hello\x00\x01\x02"
            assert parts[-1]["transport"] == "websocket" and parts[-1]["outcome"] == "complete", parts[-1]
            assert parts[-1]["close_code"] == 1000, parts[-1]
            health = json.loads((profile_root / "state/wire.json").read_text())
            assert health["parts_dropped"] == 0 and health["flows_open"] == 0, health
            check_pids()
            report["streams"] = streams
            report["websocket"] = {"chunks": chunks, "close_code": parts[-1].get("close_code")}
            report["wire_health"] = {key: health[key] for key in
                                     ("written", "flows_started", "parts_dropped", "bytes_kept")}
            report["neighbor"] = phases(NEIGHBOR)
            report["target"] = phases(TARGET)
            report["capture_phases"] = captured_phases
            report["pids_preserved"] = True
            report["scenario_passed"] = True
        except Exception:
            for name in ("capture.log", "components.log"):
                path = profile_root / "logs" / name
                if path.is_file():
                    print(str(path) + ":\n" + path.read_text(errors="replace")[-6000:],
                          file=sys.stderr)
            raise
        finally:
            try:
                if profile is not None:
                    Lifecycle(Profile.load(profile_root), adapter).off()
                    assert not adapter.service_loaded(profile)
                    assert not adapter.service_loaded(Job(profile))
            finally:
                origin_server.shutdown()
                origin_server.server_close()
            report["system_proxy_unchanged"] = adapter.network_state() == network_before
            report["cleanup_verified"] = report["system_proxy_unchanged"]

    rendered = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered)
    print(rendered)
    if not (report["scenario_passed"] and report["cleanup_verified"]):
        raise SystemExit(1)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(type(error).__name__ + ": " + str(error), file=sys.stderr)
        raise SystemExit(1)
