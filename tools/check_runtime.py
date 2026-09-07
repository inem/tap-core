#!/usr/bin/env python3
"""Opt-in live macOS test: two temporary launchd profiles, loopback HTTP only.

Creates/removes only its own uniquely named LaunchAgents. Does not set any system
proxy setting or trust a CA. Requires access to the current user's launchd domain.
"""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import platform
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tap_core.runtime import MacOS, Profile
from tap_core.records import decode_record


class Origin(BaseHTTPRequestHandler):
    def do_GET(self):
        ctype, body = {
            "/json": ("application/json", b'{"fixture":"tap-core"}'),
            "/html": ("text/html", b"<html><body>fixture</body></html>"),
            "/binary": ("application/octet-stream", b"binary-fixture"),
            "/sse": ("text/event-stream", b"data: fixture\n\n"),
            "/large": ("application/json", b'"' + b"x" * (5 * 1024 * 1024) + b'"'),
        }.get(self.path, ("text/plain", b"missing"))
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    adapter = MacOS()
    before = adapter.network_state()
    origin = ThreadingHTTPServer(("127.0.0.1", 0), Origin)
    threading.Thread(target=origin.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{origin.server_port}"
    entry = Path(__file__).resolve().parents[1] / "tap"
    profiles = []
    report = {"scope": "live macOS launchd; explicit loopback HTTP; no system routing mutation or CA trust",
              "platform": {"macOS": platform.mac_ver()[0], "architecture": platform.machine(),
                           "python": platform.python_version()}, "backend": "mitmproxy 12.2.3"}
    with tempfile.TemporaryDirectory(prefix="tap-core-live-") as directory:
        try:
            for index in range(2):
                root = Path(directory) / f"profile {index}"
                port = free_port()
                profile = Profile(root, str(args.backend.resolve()), port, "explicit", url + "/json", [])
                profiles.append(profile)
                def cli(command, *extra, profile=profile):
                    result = subprocess.run([sys.executable, str(entry), "--profile", str(profile.root), command, *extra],
                                            text=True, capture_output=True, timeout=45)
                    if result.returncode:
                        raise RuntimeError(result.stderr or result.stdout)
                    return result.stdout
                cli("install", "--backend", profile.backend, "--port", str(port), "--routing", "explicit",
                    "--probe-url", profile.probe_url)
                cli("on")
                diagnosed = json.loads(cli("doctor"))
                assert diagnosed["healthy"], diagnosed
                observed = json.loads(cli("status"))
                assert observed["port_owned"] and observed["routing"] == "explicit"
                assert json.loads(cli("where"))["data"] == str(profile.root / "data")
            first, second = profiles
            assert first.label != second.label and first.port != second.port
            second_pid = adapter.service_pid(second)
            for path in ("/json", "/html", "/binary", "/sse", "/large"):
                adapter.run(["/usr/bin/curl", "--fail", "--silent", "--noproxy", "", "--proxy",
                             f"http://127.0.0.1:{first.port}", "--output", "/dev/null", url + path])
            def records():
                return [decode_record(line, allow_legacy=False)
                        for line in (first.root / "data/stream.jsonl").read_bytes().split(b"\n")[:-1]]
            assert adapter.wait(lambda: any(record["url"].endswith("/large") for record in records()), seconds=5)
            seen = {record["url"].removeprefix(url): record for record in records()}
            assert seen["/json"]["body"] == '{"fixture":"tap-core"}'
            assert seen["/html"]["body"] == "<html><body>fixture</body></html>"
            for path in ("/binary", "/sse", "/large"):
                assert seen[path]["streamed"] and not seen[path]["body_kept"] and "body" not in seen[path]
            assert seen["/json"]["body_reason"] == seen["/html"]["body_reason"] == "retained"
            assert seen["/binary"]["body_reason"] == seen["/sse"]["body_reason"] == "media_type"
            assert seen["/large"]["body_reason"] == "streamed"
            observed_records = records()
            assert len({record["record_id"] for record in observed_records}) == len(observed_records)
            report["capture"] = {"json_html_bodies": True, "binary_sse_large_json_streamed": True,
                                 "record_version": 1, "validated_body_reasons": True, "unique_record_ids": True}
            old_pid = adapter.service_pid(first)
            assert old_pid and adapter.owns_port(first)
            os.kill(old_pid, signal.SIGTERM)  # only the verified fixture job
            assert adapter.wait(lambda: adapter.service_pid(first) not in (None, old_pid) and adapter.owns_port(first))
            report["launchd_keepalive_restart"] = True
            result = subprocess.run([sys.executable, str(entry), "--profile", str(first.root), "off"],
                                    text=True, capture_output=True, timeout=30)
            assert result.returncode == 0, result.stderr
            assert not adapter.port_open(first) and not adapter.service_loaded(first)
            assert adapter.service_pid(second) == second_pid and adapter.owns_port(second)
            report["stopping_one_profile_preserves_other"] = True
            assert not first.plist.exists()
            result = subprocess.run([sys.executable, str(entry), "--profile", str(first.root), "on"],
                                    text=True, capture_output=True, timeout=45)
            assert result.returncode == 0, result.stderr
            assert first.plist.exists() and adapter.owns_port(first)
            assert adapter.service_pid(second) == second_pid and adapter.owns_port(second)
            report["off_removes_autoload_on_recreates"] = True
            report["system_settings_unchanged"] = adapter.network_state() == before
            assert report["system_settings_unchanged"]
        finally:
            cleanup_errors = []
            for profile in profiles:
                try:
                    adapter.stop(profile)
                    profile.plist.unlink(missing_ok=True)
                except Exception as error:
                    cleanup_errors.append(f"{profile.label}: {error}")
            origin.shutdown()
            origin.server_close()
            if cleanup_errors:
                raise RuntimeError("Fixture cleanup failed: " + "; ".join(cleanup_errors))
    report["fixture_jobs_removed"] = True
    rendered = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered)
    print(rendered)


if __name__ == "__main__":
    main()
