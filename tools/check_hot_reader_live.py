#!/usr/bin/env python3
"""Real macOS hot-reader acceptance: live HTTP -> TAP proxy -> reader output.

Runs exclusively in a new temporary profile with explicit loopback routing.
Never changes the system proxy or touches an installed production profile.
Starts a real capture proxy and Hub; checks their PIDs across reader changes.
"""
import argparse
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


class Origin(BaseHTTPRequestHandler):
    def do_GET(self):
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
