#!/usr/bin/env python3
"""Opt-in acceptance for the installed migration lifecycle on macOS.

This mutates system HTTP(S) proxy settings. Run it in the owner's terminal after
reviewing the profile path. Cleanup always runs ``tap off`` and verifies direct
access; the report is written even when a check fails.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import shlex
import signal
import subprocess
import sys
import tempfile

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
from tap_core.runtime import MacOS, Profile


def normalized_network(state):
    """Ignore cached endpoint details while a proxy is disabled."""
    return {name: {"bypass": row["bypass"], **{
        kind: value if value["enabled"] else {"enabled": False}
        for kind, value in row.items() if kind != "bypass"}}
        for name, row in state.items()}


def legacy_8899_enabled(state):
    return [name for name, row in state.items()
            if any(row[kind]["enabled"] and row[kind]["port"] == 8899
                   for kind in ("http", "https"))]


def doctor_limitations(result):
    limitations = {"inspection:" + name for name in result.get("inspection_errors", {})}
    if "backend_error" in result:
        limitations.add("backend")
    if result.get("port_owned") is not True:
        limitations.add("listener_ownership")
    if (result.get("capture") or {}).get("healthy") is not True:
        limitations.add("capture")
    if result.get("traffic_probe") is not True:
        limitations.add("traffic_probe")
    bridge = result.get("bridge") or {}
    if bridge.get("healthy") is not True:
        limitations.add("bridge_snapshot")
    if (bridge.get("enabled") is True
            and (bridge.get("hub_liveness"), bridge.get("control_liveness")) != ("live", "live")):
        limitations.add("bridge_liveness")
    if (result.get("components") or {}).get("healthy") is not True:
        limitations.add("components")
    if result.get("system_proxy_verified") not in (True, "not_used"):
        limitations.add("system_proxy")
    return limitations


def default_output():
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return SOURCE / "docs/results" / f"migration-lifecycle-{stamp}.json"


class Acceptance:
    def __init__(self, args):
        self.args = args
        self.adapter = MacOS()
        self.profile = Profile.load(args.profile)
        tap = args.tap.resolve()
        self.prefix = [sys.executable, str(tap), "--profile", str(self.profile.root)]
        self.before = normalized_network(self.adapter.network_state())
        self.mutation_started = False
        self.report = {
            "version": 1,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "source_commit": subprocess.check_output(
                ["git", "-C", str(SOURCE), "rev-parse", "HEAD"], text=True).strip(),
            "executed_checkout": str(tap.parent),
            "executed_commit": subprocess.check_output(
                ["git", "-C", str(tap.parent), "rev-parse", "HEAD"], text=True).strip(),
            "host": {"macos": platform.mac_ver()[0], "architecture": platform.machine()},
            "profile": str(self.profile.root),
            "port": self.profile.port,
            "active_network_service": self.adapter.active_service(),
            "starting_network_state": self.before,
            "checks": [],
            "success": False,
            "rollback_verified": False,
        }

    def command(self, argv, *, timeout=60, allow_failure=False):
        result = subprocess.run(argv, text=True, capture_output=True, timeout=timeout)
        if result.returncode and not allow_failure:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip()
                               or f"command exited {result.returncode}: {shlex.join(argv)}")
        return result

    def cli(self, *argv, allow_failure=False):
        return self.command(self.prefix + list(argv), allow_failure=allow_failure)

    def check(self, condition, name, evidence=None):
        if not condition:
            raise RuntimeError("Failed: " + name)
        item = {"name": name, "passed": True}
        if evidence is not None:
            item["evidence"] = evidence
        self.report["checks"].append(item)
        print("OK " + name, flush=True)

    def curl(self, *extra):
        return self.command(["/usr/bin/curl", "--silent", "--show-error", "--fail",
                             "--max-time", "15", *extra, self.args.url])

    def browser_page(self):
        with tempfile.TemporaryDirectory(prefix="tap-browser-accept-") as user_data:
            argv = [str(self.args.browser), "--headless=new", "--disable-gpu",
                    "--disable-background-networking", "--no-first-run",
                    "--user-data-dir=" + user_data, "--dump-dom", self.args.url]
            process = subprocess.Popen(argv, text=True, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, start_new_session=True)
            timed_out = False
            try:
                stdout, stderr = process.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                timed_out = True
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    stdout, stderr = process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    stdout, stderr = process.communicate()
            if process.returncode and self.args.browser_expect not in stdout:
                raise RuntimeError(stderr.strip() or f"browser exited {process.returncode}")
            return stdout, timed_out

    def direct_baseline(self, label):
        state = normalized_network(self.adapter.network_state())
        self.check(not any(row[kind]["enabled"] for row in state.values()
                           for kind in ("http", "https")), label + " proxy disabled")
        result = self.curl("--noproxy", "*")
        self.check(bool(result.stdout), label + " direct network access")

    def off_to_baseline(self):
        """Use normal recovery, or narrowly rescue an unowned profile endpoint."""
        result = self.cli("off", allow_failure=True)
        if result.returncode == 0:
            return
        state = self.adapter.network_state()
        enabled = [(service, kind, row[kind]) for service, row in state.items()
                   for kind in ("http", "https") if row[kind]["enabled"]]
        expected = ("127.0.0.1", self.profile.port)
        if not enabled or any((value["server"], value["port"]) != expected
                              for _, _, value in enabled):
            raise RuntimeError(result.stderr.strip() or "tap off failed")
        for service, kind, value in enabled:
            disabled = dict(value)
            disabled["enabled"] = False
            self.adapter.set_proxy(service, kind == "https", disabled)
        self.report["unowned_profile_proxy_rescued"] = [
            {"service": service, "kind": kind} for service, kind, _ in enabled]
        self.cli("off")

    def run(self):
        if self.profile.routing != "system":
            raise RuntimeError("Profile must use system routing")
        if self.profile.port == 8899:
            raise RuntimeError("Refusing to exercise legacy port 8899")
        legacy = legacy_8899_enabled(self.before)
        if legacy:
            raise RuntimeError("Legacy 8899 is enabled on: " + ", ".join(legacy))
        self.mutation_started = True
        self.off_to_baseline()
        self.direct_baseline("baseline")
        self.cli("on")

        status = json.loads(self.cli("status", "--output", "raw-json").stdout)
        self.check(status["port"] == self.profile.port and status["port_open"]
                   and status["port_owned"] and status["service_loaded"],
                   "listener is owned", {k: status.get(k) for k in
                   ("port", "pid", "port_open", "port_owned", "service_loaded")})
        explicit = self.curl("--noproxy", "", "--proxy", f"http://127.0.0.1:{self.profile.port}")
        self.check(bool(explicit.stdout), "explicit HTTPS proxy request")
        self.check(status["system_proxy_verified"] is True,
                   "all enabled network services use tap-core")
        scutil = self.command(["/usr/sbin/scutil", "--proxy"]).stdout
        self.check("HTTPEnable : 1" in scutil and "HTTPSEnable : 1" in scutil
                   and "127.0.0.1" in scutil and str(self.profile.port) in scutil,
                   "scutil reports the tap-core proxy", scutil)
        ordinary = self.curl()
        self.check(bool(ordinary.stdout), "ordinary HTTPS request through system routing")

        doctor_run = self.cli("doctor", "--output", "raw-json", allow_failure=True)
        doctor = json.loads(doctor_run.stdout)
        expected = set(self.args.expected_limitation)
        actual = doctor_limitations(doctor)
        unexpected = actual - expected
        accepted_limitations = bool(expected) and bool(actual) and not unexpected
        self.check(doctor.get("healthy") is True or accepted_limitations,
                   "doctor healthy or only named limitations",
                   {"healthy": doctor.get("healthy"), "expected": sorted(expected),
                    "observed": sorted(actual), "unexpected": sorted(unexpected)})

        browser_html, browser_timed_out = self.browser_page()
        self.check(self.args.browser_expect in browser_html,
                   "browser page access", {"expected_text": self.args.browser_expect,
                                           "terminated_after_dom": browser_timed_out})
        self.report["success"] = True

    def cleanup(self):
        if not self.mutation_started:
            return
        try:
            self.cli("off")
            self.direct_baseline("rollback")
            self.report["rollback_verified"] = True
        except BaseException as error:
            self.report["rollback_error"] = type(error).__name__ + ": " + str(error)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, default=Path.home() / ".tap-core/profile")
    parser.add_argument("--tap", type=Path, default=SOURCE / "tap")
    parser.add_argument("--browser", type=Path, required=True,
                        help="Chrome/Chromium executable; launched without proxy overrides")
    parser.add_argument("--url", default="https://example.com/")
    parser.add_argument("--browser-expect", default="Example Domain")
    parser.add_argument("--expected-limitation", action="append", default=[])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--allow-system-routing", action="store_true", required=True)
    args = parser.parse_args()
    output = args.output or default_output()
    acceptance = None
    try:
        acceptance = Acceptance(args)
        acceptance.run()
    except BaseException as error:
        if acceptance is None:
            raise
        acceptance.report["error"] = type(error).__name__ + ": " + str(error)
    finally:
        if acceptance is not None:
            acceptance.cleanup()
            acceptance.report["finished_at"] = datetime.now(timezone.utc).isoformat()
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(acceptance.report, indent=2) + "\n")
            print("Report: " + str(output), flush=True)
    return 0 if acceptance.report["success"] and acceptance.report["rollback_verified"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
