#!/usr/bin/env python3
"""Characterize selected trusted legacy TAP files using synthetic local fixtures.

No system proxy changes, external requests, real account data or production Hub.
This runs source supplied by the caller; it is not an untrusted-code sandbox.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qsl, urlsplit

sys.dont_write_bytecode = True


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CaptureResponse:
    def __init__(self, ctype, body, forbid_body=False):
        self.headers = {"content-type": ctype, "content-length": str(len(body))}
        self.status_code = 200
        self.stream = False
        self.body = body
        self.forbid_body = forbid_body

    @property
    def content(self):
        if self.forbid_body:
            raise AssertionError("streamed response body was accessed")
        return self.body.encode()

    def get_text(self):
        if self.forbid_body:
            raise AssertionError("streamed response body was decoded")
        return self.body


def characterize_capture(source, root):
    real_expanduser = os.path.expanduser

    def isolated_path(value):
        if value.startswith("~/.tap/"):
            return str(root / value.removeprefix("~/.tap/"))
        return real_expanduser(value)

    # Only redirect the legacy module's two fixed filesystem destinations.
    # The unmodified hooks and background writer still execute.
    with patch("os.path.expanduser", side_effect=isolated_path):
        capture = load_module("legacy_capture", source / "capture.py")
    cases = [
        ("application/json", '{"fixture":true}', True),
        ("text/html", "<html><body>fixture</body></html>", True),
        ("application/octet-stream", "binary-fixture", False),
        ("text/event-stream", "data: fixture\n\n", False),
    ]
    for index, (ctype, body, keep) in enumerate(cases):
        request = SimpleNamespace(
            method="GET", url=f"https://example.test/fixture/{index}",
            headers={"user-agent": "tap-synthetic-fixture"}, get_text=lambda: "",
        )
        response = CaptureResponse(ctype, body, forbid_body=not keep)
        flow = SimpleNamespace(request=request, response=response)
        capture.responseheaders(flow)
        assert response.stream is (not keep)
        capture.response(flow)
    with capture._q.all_tasks_done:
        drained = capture._q.all_tasks_done.wait_for(
            lambda: capture._q.unfinished_tasks == 0, timeout=5,
        )
    assert drained, "capture writer did not drain"
    records = [json.loads(line) for line in (root / "stream.jsonl").read_text().splitlines()]
    assert len(records) == len(cases)
    for record, (ctype, body, keep) in zip(records, cases):
        assert record["ctype"] == ctype
        assert record["body_kept"] is keep
        assert record["streamed"] is (not keep)
        assert ("body" in record) is keep
        if keep:
            assert record["body"] == body
    return {"status": "verified_fixture", "records": len(records),
            "body_access_for_streamed_responses": False}


def characterize_reader(source, root):
    cid = "00000000-0000-4000-8000-000000000001"
    env = os.environ.copy()
    env["TAP_OUT"] = str(root)

    def send(title):
        body = {"mapping": {}, "title": title, "current_node": None}
        record = {"url": f"https://example.test/backend-api/conversations/{cid}",
                  "status": 200, "body": json.dumps(body)}
        result = subprocess.run(
            [sys.executable, str(source / "readers/chatgpt")],
            input=json.dumps(record) + "\n", env=env,
            text=True, capture_output=True, timeout=10,
        )
        assert result.returncode == 0, result.stderr
        return json.loads((root / "chatgpt" / f"{cid}.json").read_text())["title"]

    assert send("Fixture A") == "Fixture A"
    assert send("Fixture B") == "Fixture B"
    latest_after_repeat = send("Fixture A")
    assert latest_after_repeat in ("Fixture A", "Fixture B"), "unexpected latest value"
    return {
        "status": "verified_fixture", "first_store_and_new_version": True,
        "repeat_a_after_b": "known_gap" if latest_after_repeat == "Fixture B" else "resolved",
        "expected_latest": "Fixture A", "observed_latest": latest_after_repeat,
    }


class PageResponse:
    def __init__(self):
        self.headers = {"content-type": "text/html"}
        self.body = '<html><body><script nonce="YWJjZA==">0</script></body></html>'

    def get_text(self, strict=False):
        return self.body

    def set_text(self, value):
        self.body = value


def page_request(host="example.test", path="/"):
    return SimpleNamespace(
        scheme="https", host=host, pretty_host=host, port=443, path=path,
        headers={"sec-fetch-dest": "document"},
        query=dict(parse_qsl(urlsplit(path).query)),
    )


def characterize_injection(source, root):
    root.mkdir()
    (root / "token").write_text("synthetic-fixture-token")
    (root / "allowlist.txt").write_text("https://example.test\nhttps://second.test\n")
    with patch.dict(os.environ, {"TAP_PROBE_ROOT": str(root), "TAP_PROBE_PORT": "47991",
                                 "TAP_PROBE_HOST": "127.0.0.1"}):
        injector = load_module("legacy_injector", source / "mutators/site-probe.py")
    for host in ("example.test", "second.test"):
        flow = SimpleNamespace(request=page_request(host), response=PageResponse(), metadata={})
        injector.response(flow)
        assert flow.response.body.count('id="tap-probe-bootstrap"') == 1
        assert 'nonce="YWJjZA=="' in flow.response.body
        before = flow.response.body
        injector.response(flow)
        assert flow.response.body == before
    denied = SimpleNamespace(request=page_request("denied.test"), response=PageResponse(), metadata={})
    injector.response(denied)
    assert "tap-probe-bootstrap" not in denied.response.body
    request = page_request(path="/__tap/probe/ws?token=synthetic-fixture-token&page=fixture")
    request.headers.update({"cookie": "synthetic", "authorization": "Bearer synthetic",
                            "proxy-authorization": "synthetic"})
    flow = SimpleNamespace(request=request, response=None, metadata={})
    injector.requestheaders(flow)
    assert request.host == "127.0.0.1" and request.port == 47991
    assert "token=" not in request.path
    assert all(key not in request.headers for key in ("cookie", "authorization", "proxy-authorization"))
    return {"status": "verified_fixture", "allowed_origins": 2,
            "denied_origin_unchanged": True, "duplicate_bootstrap_prevented": True,
            "route_credentials_scrubbed": True, "live_websocket": "not_tested"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path,
                        help="Trusted legacy source tree or local snapshot")
    args = parser.parse_args()
    source = args.source.resolve()
    files = ("capture.py", "readers/chatgpt", "mutators/site-probe.py")
    hashes = {name: hashlib.sha256((source / name).read_bytes()).hexdigest() for name in files}
    with tempfile.TemporaryDirectory(prefix="tap-characterize-") as directory:
        root = Path(directory)
        report = {
            "scope": "synthetic fixtures against unmodified legacy source; no live WS or clean-Mac acceptance",
            "source_sha256": hashes,
            "capture": characterize_capture(source, root / "capture"),
            "reader": characterize_reader(source, root / "reader"),
            "injection": characterize_injection(source, root / "probe"),
        }
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
