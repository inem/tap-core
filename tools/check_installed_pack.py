#!/usr/bin/env python3
"""Opt-in #14 live check: installed external page pack lifecycle + live injection.

Landable slice only. Uses EXPLICIT routing on a unique loopback port and a
unique launchd label, so it never mutates the system proxy, never needs sudo,
and does not touch the owner's existing TAP install. Browser-less: it verifies
that an installed+enabled page pack injects into a real proxied HTML response
and that user grants/exclusions decide origins on both the proxy bridge and the
`bridge explain` view. The page<->Hub WebSocket round trip and installed
reader/handler entrypoints are NOT exercised here (need a browser / not yet
runtime-activated) and are reported as not_tested. Cleanup always runs; the
report's cleanup_verified gates success.
"""
import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import platform
import socket
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tap_core.runtime import MacOS, Profile

SENTINEL = "tap-pack-check-" + hashlib.sha256(b"installed-page").hexdigest()[:10]
UI_JS = f"window.__tapPackCheck = {json.dumps(SENTINEL)};\n"


class Origin(BaseHTTPRequestHandler):
    HTML = ("<!doctype html><html><head><title>tap pack check</title></head>"
            "<body><h1>fixture origin</h1></body></html>")

    def do_GET(self):
        body = self.HTML.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait(predicate, label, seconds=20):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    raise RuntimeError("Timed out: " + label)


def run(argv, timeout=60, check=True):
    result = subprocess.run(list(map(str, argv)), text=True, capture_output=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError((result.stderr or result.stdout or "command failed").strip())
    return result


def fetch(url, proxy_port=None, ca=None):
    argv = ["/usr/bin/curl", "--silent", "--show-error", "--max-time", "10", url]
    if proxy_port is not None:
        argv[1:1] = ["--proxy", f"http://127.0.0.1:{proxy_port}"]
    if ca is not None:
        argv[1:1] = ["--cacert", str(ca)]
    return run(argv).stdout


def build_pack(source, origin):
    source.mkdir(parents=True)
    (source / "ui.js").write_text(UI_JS)
    manifest = {
        "manifest_version": 1,
        "id": "tap.check.installed-page",
        "version": "0.1.0",
        "requires": {"pack_api": 1, "dependencies": []},
        "files": ["ui.js"],
        "resources": [{
            "contract": "tap.page-resource/v1", "id": "check.ui", "version": "1.0.0",
            "kind": "browser-classic-script", "file": "ui.js",
            "sha256": hashlib.sha256(UI_JS.encode()).hexdigest(),
            "license": "MIT", "source_revision": "check-v1",
        }],
        "entrypoints": {"page": {"interface": "browser-scripts-v1",
                                 "uses": [{"id": "check.ui", "version": "1.0.0"}]}},
        "config": {},
        "access": {"origins": [origin], "capabilities": ["page.inject"]},
    }
    (source / "pack.json").write_text(json.dumps(manifest, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", type=Path, required=True, help="mitmdump 12.2.3 executable")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    # Child pythons (`-m tap_core.pack_store`) must import the checkout package.
    os.environ["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT), os.environ.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    backend = args.backend.resolve(strict=True)
    adapter = MacOS()
    network_before = adapter.network_state()
    report = {
        "scope": "live loopback HTTP injection through an installed external page pack, "
                 "explicit routing in an isolated launchd profile; synthetic data; no system proxy, no sudo",
        "platform": {"macOS": platform.mac_ver()[0], "architecture": platform.machine(),
                     "python": platform.python_version()},
        "backend": run([backend, "--version"]).stdout.splitlines()[0],
        "steps": {},
        "transport": {"http_body_injection": "not_tested", "own_page_hub_ws": "not_tested",
                      "installed_reader_entrypoint": "not_tested (role not runtime-activated)",
                      "installed_handler_entrypoint": "not_tested (role not runtime-activated)",
                      "https_ca_trust": "not_tested", "sse": "not_tested",
                      "third_party_ws_capture": "not_tested"},
        "cleanup_verified": False,
    }
    servers, profile = [], None
    with tempfile.TemporaryDirectory(prefix="tap-installed-pack-") as directory:
        root = Path(directory)
        root.chmod(0o700)
        try:
            server = ThreadingHTTPServer(("127.0.0.1", 0), Origin)
            servers.append(server)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            origin = f"http://127.0.0.1:{server.server_port}"
            excluded = f"http://127.0.0.1:{free_port()}"

            source = root / "pack-src"
            build_pack(source, origin)
            artifact = root / "check.tap-pack"
            run([sys.executable, "-m", "tap_core.pack_store", "build", source, "--output", artifact])

            profile = Profile(root / "profile", str(backend), free_port(), "explicit", origin + "/", [])
            prefix = [sys.executable, ROOT / "tap", "--profile", profile.root]
            bridge_config = {"version": 1, "enabled": True, "hub_port": free_port(),
                             "allow_origins": [], "exclude_origins": [excluded], "page_scripts": []}
            bridge_path = root / "bridge-config.json"
            bridge_path.write_text(json.dumps(bridge_config))

            run(prefix + ["install", "--backend", backend, "--port", profile.port,
                          "--routing", "explicit", "--probe-url", origin + "/", "--bridge-config", bridge_path])
            profile = Profile.load(profile.root)
            report["steps"]["install"] = "ok"

            # Packs may only change while the profile is stopped.
            run(prefix + ["off"])
            installed = json.loads(run(prefix + ["pack", "install", artifact]).stdout)
            report["steps"]["pack_install"] = {"id": installed.get("id"), "version": installed.get("version")}

            # Enable with the matching user grant; the manifest requests `origin`.
            run(prefix + ["pack", "enable", "tap.check.installed-page", "--version", "0.1.0",
                          "--grant-origin", origin, "--grant-capability", "page.inject"])
            report["steps"]["pack_enable"] = "ok"

            run(prefix + ["on"])  # restart with the pack enabled so injection applies
            report["steps"]["on"] = "ok"
            ca = profile.root / "certificates/mitmproxy-ca-cert.pem"

            direct = fetch(origin + "/")
            proxied = wait(lambda: (fetch(origin + "/", profile.port, ca) or None), "proxied fetch")
            injected = ("__tap" in proxied or "/__tap/" in proxied or SENTINEL in proxied
                        or ("<script" in proxied and proxied != direct))
            report["steps"]["injection_on_granted_origin"] = "verified_live" if injected else "NOT injected"
            report["injected_bytes_delta"] = len(proxied) - len(direct)
            if injected:
                report["transport"]["http_body_injection"] = "verified_live"

            # bridge explain: granted origin allowed; excluded origin overridden by user.
            allowed = json.loads(run(prefix + ["bridge", "explain", "--origin", origin]).stdout)
            denied = json.loads(run(prefix + ["bridge", "explain", "--origin", excluded]).stdout)
            report["steps"]["bridge_explain"] = {
                "granted_allowed": bool(allowed.get("allowed")),
                "excluded_reason": denied.get("reason"),
                "excluded_blocked": not denied.get("allowed"),
            }

            run(prefix + ["off"])
            report["steps"]["off"] = "ok"
            report["steps"]["pack_disable"] = json.loads(run(prefix + ["pack", "disable", "tap.check.installed-page"]).stdout)
            report["steps"]["pack_list_after_disable"] = json.loads(run(prefix + ["pack", "list"]).stdout)
            uninstalled = json.loads(run(prefix + ["pack", "uninstall", "tap.check.installed-page"]).stdout)
            report["steps"]["pack_uninstall"] = {k: uninstalled.get(k) for k in
                                                 ("removed_versions", "data_retained", "state_retained", "logs_retained")}
            report["steps"]["profile_data_retained"] = (profile.root / "data").is_dir()
        except Exception as error:
            report["error"] = str(error)
            for label, path in [("proxy", root / "profile/logs/capture.log")]:
                if path.exists():
                    print(label + ":\n" + path.read_text(errors="replace")[-8000:], file=sys.stderr)
        finally:
            cleanup_errors = []
            if profile is not None:
                try:
                    adapter.stop(profile)
                    profile.plist.unlink(missing_ok=True)
                    if adapter.service_loaded(profile) or adapter.port_open(profile):
                        cleanup_errors.append("service or port still active")
                except Exception as error:
                    cleanup_errors.append(str(error))
            for server in servers:
                server.shutdown()
                server.server_close()
            report["system_settings_unchanged"] = adapter.network_state() == network_before
            if not report["system_settings_unchanged"]:
                cleanup_errors.append("system network settings changed")
            report["cleanup_errors"] = cleanup_errors
            report["cleanup_verified"] = not cleanup_errors and "error" not in report
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0 if report["cleanup_verified"] and "error" not in report else 1


if __name__ == "__main__":
    sys.exit(main())
