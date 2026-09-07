#!/usr/bin/env python3
"""Opt-in #14 live check: installed external page pack lifecycle + live injection.

Landable slice only. Uses EXPLICIT routing on a unique loopback port and a
unique launchd label, so it never mutates the system proxy, never needs sudo,
and does not touch the owner's existing TAP install. Browser-less: it verifies
that an installed+enabled page pack injects into a real proxied HTML response,
that the pack's actual page resource is served over the reserved token route,
and that a user exclusion overrides a pack grant on the proxy bridge itself.

Two results are reported separately: scenario_passed (every claimed behavior was
asserted, not merely recorded) and cleanup_verified (the temporary service and
network state were restored). Exit is 0 only when BOTH hold. If cleanup could not
be confirmed, the temporary profile is PRESERVED with a recovery command instead
of being deleted. The page<->Hub WebSocket round trip and installed
reader/handler entrypoints are reported as not_tested (need a browser / roles not
yet runtime-activated).
"""
import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tap_core.runtime import MacOS, Profile

MARKER = "tap-probe-bootstrap"
PACK_ID = "tap.check.installed-page"
BODY_MARK = "tap-fixture-origin-body"
SENTINEL = "tap-pack-check-" + hashlib.sha256(b"installed-page").hexdigest()[:10]
UI_JS = f"window.__tapPackCheck = {json.dumps(SENTINEL)};\n"
DATA_SENTINEL = "retained-" + hashlib.sha256(b"data").hexdigest()[:10]


class Origin(BaseHTTPRequestHandler):
    HTML = ("<!doctype html><html><head><title>tap pack check</title></head>"
            f"<body><h1>{BODY_MARK}</h1></body></html>")

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


def http_get(url, proxy_port=None, ca=None):
    """Return (status_code, body). Status is checked so an error page (502/403)
    can never pass a body assertion by merely lacking the injection marker."""
    argv = ["/usr/bin/curl", "--silent", "--show-error", "--max-time", "10", "-o", "-", "-w", "\\n%{http_code}", url]
    if proxy_port is not None:
        argv[1:1] = ["--proxy", f"http://127.0.0.1:{proxy_port}"]
    if ca is not None:
        argv[1:1] = ["--cacert", str(ca)]
    out = run(argv).stdout
    body, _, code = out.rpartition("\n")
    return (int(code) if code.isdigit() else 0), body


def build_pack(source, origins):
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
        "access": {"origins": list(origins), "capabilities": ["page.inject"]},
    }
    (source / "pack.json").write_text(json.dumps(manifest, indent=2))


def scenario_failures(steps):
    """Pure gate over collected results. Every claimed behavior must hold.

    Kept importable and side-effect free so the harness has negative tests.
    """
    failures = []
    def need(key, ok, detail):
        if not ok:
            failures.append(f"{key}: {detail}")
    need("install", steps.get("install") == "ok", "profile install did not report ok")
    need("pack_install", (steps.get("pack_install") or {}).get("version") == "0.1.0",
         "pack install did not report the expected version")
    need("pack_enable", steps.get("pack_enable") == "ok", "pack enable did not report ok")
    need("on", steps.get("on") == "ok", "profile on did not report ok")
    need("injection_granted_marker", steps.get("injection_granted_marker") is True,
         "granted origin response did not contain the bridge bootstrap marker")
    need("installed_resource_served", steps.get("installed_resource_served") is True,
         "the pack's own ui.js was not served byte-for-byte over the reserved route")
    need("exclusion_suppresses_injection", steps.get("exclusion_suppresses_injection") is True,
         "a granted-but-excluded origin was still injected on the proxy bridge")
    explain = steps.get("bridge_explain") or {}
    need("explain_granted_allowed", explain.get("granted_allowed") is True, "explain did not allow the granted origin")
    need("explain_exclusion", explain.get("excluded_reason") == "user_exclusion" and explain.get("excluded_blocked") is True,
         "explain did not block the excluded origin as user_exclusion")
    need("pack_disable", (steps.get("pack_disable") or {}).get("enabled") is False, "pack disable did not clear enabled")
    need("pack_uninstall", "0.1.0" in ((steps.get("pack_uninstall") or {}).get("removed_versions") or []),
         "pack uninstall did not remove 0.1.0")
    need("data_retained", steps.get("data_sentinel_retained") is True,
         "a sentinel in the pack's own state/data dirs was lost on uninstall")
    return failures


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", type=Path, required=True, help="mitmdump 12.2.3 executable")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    os.environ["PYTHONPATH"] = os.pathsep.join([str(ROOT), os.environ.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    backend = args.backend.resolve(strict=True)
    adapter = MacOS()
    network_before = adapter.network_state()
    report = {
        "scope": "live loopback HTTP injection + reserved-route resource delivery + exclusion override through an "
                 "installed external page pack; explicit routing in an isolated launchd profile; synthetic data; "
                 "no system proxy, no sudo",
        "commit": run(["/usr/bin/git", "-C", ROOT, "rev-parse", "HEAD"], check=False).stdout.strip() or "unknown",
        "platform": {"macOS": platform.mac_ver()[0], "architecture": platform.machine(), "python": platform.python_version()},
        "backend": run([backend, "--version"]).stdout.splitlines()[0],
        "steps": {},
        "transport": {"http_body_injection": "not_tested", "reserved_route_resource_delivery": "not_tested",
                      "exclusion_override_on_proxy_bridge": "not_tested", "own_page_hub_ws": "not_tested",
                      "installed_reader_entrypoint": "not_tested (role not runtime-activated)",
                      "installed_handler_entrypoint": "not_tested (role not runtime-activated)",
                      "https_ca_trust": "not_tested", "sse": "not_tested", "third_party_ws_capture": "not_tested"},
        "scenario_passed": False, "cleanup_verified": False,
    }
    steps = report["steps"]
    servers, profile = [], None
    root = Path(tempfile.mkdtemp(prefix="tap-installed-pack-"))
    root.chmod(0o700)
    try:
        injected_origin, excluded_origin = None, None
        for _ in range(2):
            server = ThreadingHTTPServer(("127.0.0.1", 0), Origin)
            servers.append(server)
            threading.Thread(target=server.serve_forever, daemon=True).start()
        injected_origin = f"http://127.0.0.1:{servers[0].server_port}"
        excluded_origin = f"http://127.0.0.1:{servers[1].server_port}"

        source = root / "pack-src"
        # The pack requests BOTH origins; the user grants both; the profile bridge
        # excludes one. That makes the exclusion a real override of a pack grant.
        build_pack(source, [injected_origin, excluded_origin])
        artifact = root / "check.tap-pack"
        run([sys.executable, "-m", "tap_core.pack_store", "build", source, "--output", artifact])

        profile = Profile(root / "profile", str(backend), free_port(), "explicit", injected_origin + "/", [])
        prefix = [sys.executable, ROOT / "tap", "--profile", profile.root]
        bridge_config = {"version": 1, "enabled": True, "hub_port": free_port(),
                         "allow_origins": [], "exclude_origins": [excluded_origin], "page_scripts": []}
        bridge_path = root / "bridge-config.json"
        bridge_path.write_text(json.dumps(bridge_config))

        run(prefix + ["install", "--backend", backend, "--port", profile.port,
                      "--routing", "explicit", "--probe-url", injected_origin + "/", "--bridge-config", bridge_path])
        profile = Profile.load(profile.root)
        steps["install"] = "ok"

        run(prefix + ["off"])  # packs may only change while the profile is stopped
        installed = json.loads(run(prefix + ["pack", "install", artifact]).stdout)
        steps["pack_install"] = {"id": installed.get("id"), "version": installed.get("version")}
        run(prefix + ["pack", "enable", "tap.check.installed-page", "--version", "0.1.0",
                      "--grant-origin", injected_origin, "--grant-origin", excluded_origin,
                      "--grant-capability", "page.inject"])
        steps["pack_enable"] = "ok"
        run(prefix + ["on"])
        steps["on"] = "ok"

        ca = profile.root / "certificates/mitmproxy-ca-cert.pem"
        token = (profile.root / "state/bridge-token").read_text().strip()

        def granted_ready():
            code, body = http_get(injected_origin + "/", profile.port, ca)
            return (code, body) if code == 200 and body else None
        _, granted_html = wait(granted_ready, "granted-origin fetch")
        # 200 + the origin's real body + injection markers: an error page can
        # never masquerade as "injected" (or, below, as "suppressed").
        steps["injection_granted_marker"] = (BODY_MARK in granted_html and MARKER in granted_html
                                             and "core/0.js" in granted_html)
        report["transport"]["http_body_injection"] = ("verified_live" if steps["injection_granted_marker"]
                                                       else "NOT injected")

        # Fetch the pack's OWN resource over the reserved token route and compare bytes.
        res_code, served = http_get(f"{injected_origin}/__tap/probe/core/0.js?token={token}", profile.port, ca)
        steps["installed_resource_served"] = res_code == 200 and served == UI_JS
        report["installed_resource_sha256_match"] = (
            hashlib.sha256(served.encode()).hexdigest() == hashlib.sha256(UI_JS.encode()).hexdigest())
        if steps["installed_resource_served"]:
            report["transport"]["reserved_route_resource_delivery"] = "verified_live"

        # Granted-but-excluded origin must serve its real body (HTTP 200 + body)
        # but NOT be injected on the proxy bridge. Requiring 200+body rejects a
        # 502/403/empty false pass.
        exc_code, excluded_html = http_get(excluded_origin + "/", profile.port, ca)
        steps["exclusion_suppresses_injection"] = (exc_code == 200 and BODY_MARK in excluded_html
                                                   and MARKER not in excluded_html)
        if steps["exclusion_suppresses_injection"]:
            report["transport"]["exclusion_override_on_proxy_bridge"] = "verified_live"

        allowed = json.loads(run(prefix + ["bridge", "explain", "--origin", injected_origin]).stdout)
        denied = json.loads(run(prefix + ["bridge", "explain", "--origin", excluded_origin]).stdout)
        steps["bridge_explain"] = {"granted_allowed": bool(allowed.get("allowed")),
                                   "excluded_reason": denied.get("reason"),
                                   "excluded_blocked": not denied.get("allowed")}

        run(prefix + ["off"])
        steps["off"] = "ok"
        # Sentinels in the pack's OWN state/data directories (not the general
        # profile data dir), which uninstall claims to retain.
        pack_dirs = {kind: profile.root / kind / "packs" / PACK_ID for kind in ("state", "data", "logs")}
        for path in pack_dirs.values():
            path.mkdir(parents=True, exist_ok=True)
            (path / "sentinel.txt").write_text(DATA_SENTINEL)
        steps["pack_disable"] = json.loads(run(prefix + ["pack", "disable", PACK_ID]).stdout)
        steps["pack_uninstall"] = json.loads(run(prefix + ["pack", "uninstall", PACK_ID]).stdout)
        steps["data_sentinel_retained"] = all(
            (path / "sentinel.txt").is_file() and (path / "sentinel.txt").read_text() == DATA_SENTINEL
            for path in pack_dirs.values())
    except Exception as error:
        report["error"] = str(error)
        log = root / "profile/logs/capture.log"
        if log.exists():
            print("proxy:\n" + log.read_text(errors="replace")[-8000:], file=sys.stderr)
    finally:
        cleanup_errors = []
        stop_confirmed = True
        if profile is not None:
            try:
                adapter.stop(profile)
                profile.plist.unlink(missing_ok=True)
                if adapter.service_loaded(profile) or adapter.port_open(profile):
                    stop_confirmed = False
                    cleanup_errors.append("service or port still active")
            except Exception as error:
                stop_confirmed = False
                cleanup_errors.append(str(error))
        for server in servers:
            server.shutdown()
            server.server_close()
        report["system_settings_unchanged"] = adapter.network_state() == network_before
        if not report["system_settings_unchanged"]:
            cleanup_errors.append("system network settings changed")
        report["cleanup_errors"] = cleanup_errors
        report["cleanup_verified"] = not cleanup_errors and "error" not in report
        report["scenario_passed"] = "error" not in report and not scenario_failures(steps)
        report["scenario_failures"] = scenario_failures(steps)
        # Recovery-first: only delete the temporary profile once stop is confirmed.
        if stop_confirmed and report["system_settings_unchanged"]:
            shutil.rmtree(root, ignore_errors=True)
            report["temporary_profile_removed"] = True
        else:
            report["temporary_profile_preserved"] = str(root / "profile")
            report["recovery_command"] = f"{sys.executable} {ROOT / 'tap'} --profile {root / 'profile'} off"
            report["temporary_profile_removed"] = False
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0 if report["scenario_passed"] and report["cleanup_verified"] else 1


if __name__ == "__main__":
    sys.exit(main())
