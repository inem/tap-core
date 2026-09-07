#!/usr/bin/env python3
"""Run trusted legacy on/off/install branches against synthetic operations.

Requires a separately supplied, hash-pinned private source snapshot. Never
publishes that source. This is a characterization fixture, not a code sandbox
or evidence of working macOS installation/network integration.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile


SOURCE_SHA256 = "a29a7ca35e3eff188cdf037eb7a59ae4518ed73fab02f09731252756b45d007e"
DISPATCH = '\ncase "${1:-status}" in\n'

# Inject after the original function definitions, before its unchanged command
# dispatcher. Every operation reachable in these three branches is substituted.
# HOME stays unchanged: mkdir/ln are no-op functions, not real filesystem calls.
ADAPTERS = r'''
REPO="$TAP_FIXTURE_ROOT"
ADDON="$REPO/capture.py"
PLIST="$REPO/service.plist"
LOG="$REPO/tap.log"
STREAM="$REPO/stream.jsonl"
TAPDIR="$REPO"
NS="$REPO/never-execute-networksetup"
event() { printf '%s\n' "$1" >> "$TAP_FIXTURE_EVENTS"; }
start_proc() { event start; return "${TAP_FIXTURE_START:-0}"; }
arm_proxy() { event arm; return "${TAP_FIXTURE_ARM:-0}"; }
disarm_proxy() { event disarm; return "${TAP_FIXTURE_DISARM:-0}"; }
flows() { event flows; return "${TAP_FIXTURE_FLOWS:-0}"; }
stop_proc() { event stop; return "${TAP_FIXTURE_STOP:-0}"; }
port_up() { event port; return "${TAP_FIXTURE_PORT:-0}"; }
armed() { event armed; return "${TAP_FIXTURE_ARMED:-0}"; }
active_svc() { printf 'Fixture Wi-Fi\n'; }
write_plist() { event plist; return "${TAP_FIXTURE_PLIST:-0}"; }
mkdir() { event mkdir; return 0; }
ln() { event link; return 0; }
sleep() { :; }
id() { case "$1" in -u) printf '501\n';; -un) printf 'fixture\n';; *) exit 97;; esac; }
command() {
  if [ "$#" -eq 2 ] && [ "$1" = -v ] && [ "$2" = mitmdump ]; then
    event backend
    [ "${TAP_FIXTURE_BACKEND:-0}" = 0 ] || return 1
    printf '%s/mitmdump\n' "$TAP_FIXTURE_ROOT"
  else
    event unexpected-command; exit 97
  fi
}
launchctl() {
  event "launchctl:$1"
  case "$1" in
    bootout) return 0;;
    bootstrap) return "${TAP_FIXTURE_BOOTSTRAP:-0}";;
    print) return "${TAP_FIXTURE_SERVICE:-0}";;
    *) exit 97;;
  esac
}
sudo() {
  # Only the permission inquiry is reachable with lifecycle helpers replaced.
  if [ "$#" -eq 6 ] && [ "$1" = -n ] && [ "$2" = -l ] &&
     [ "$3" = "$NS" ] && [ "$4" = -setwebproxystate ] && [ "$6" = off ]; then
    event permission; return "${TAP_FIXTURE_PERMISSION:-0}"
  fi
  event unexpected-sudo; exit 97
}
security() {
  if [ "$#" -eq 3 ] && [ "$1" = find-certificate ] && [ "$2" = -c ] && [ "$3" = mitmproxy ]; then
    event certificate; return "${TAP_FIXTURE_CERTIFICATE:-0}"
  fi
  event unexpected-security; exit 97
}
'''

INSTALL_PREFIX = ["backend", "mkdir", "link", "plist", "launchctl:bootout",
                  "stop", "launchctl:bootstrap"]
INSTALL_SUFFIX = ["permission", "armed", "certificate"]


def case(name, command, events, text, *, codes=None, exit_code=0, gap=None):
    return dict(name=name, command=command, events=events, text=text,
                codes=codes or {}, exit_code=exit_code, gap=gap)


def scenarios():
    successful_install = INSTALL_PREFIX + ["port", "launchctl:print"] + INSTALL_SUFFIX
    failed_install = INSTALL_PREFIX + ["port"] * 12 + ["launchctl:print", "disarm"] + INSTALL_SUFFIX
    return [
        case("on_success", "on", ["start", "arm", "flows"], "ON —"),
        case("on_start_failure", "on", ["start"], "proxy untouched",
             codes={"START": 1}, exit_code=1),
        case("on_arm_failure", "on", ["start", "arm"], "proxy is off, net safe",
             codes={"ARM": 1}, exit_code=1,
             gap="Arm failure returns without attempting rollback; the off claim is unverified."),
        case("on_probe_failure", "on", ["start", "arm", "flows", "disarm"],
             "disarmed, net safe", codes={"FLOWS": 1}, exit_code=1),
        case("on_rollback_failure", "on", ["start", "arm", "flows", "disarm"],
             "disarmed, net safe", codes={"FLOWS": 1, "DISARM": 1}, exit_code=1,
             gap="Failed rollback is still reported as disarmed and safe."),
        case("off_success", "off", ["disarm", "stop"], "recorder may respawn"),
        case("off_disarm_failure", "off", ["disarm"], "REFUSING to kill",
             codes={"DISARM": 1}, exit_code=1),
        case("off_stop_failure", "off", ["disarm", "stop"], "traffic goes direct",
             codes={"STOP": 1}),
        case("install_missing_backend", "install", ["backend"], "mitmproxy not installed",
             codes={"BACKEND": 1}, exit_code=1),
        case("install_success", "install", successful_install, "loaded, capture live"),
        case("install_start_failure", "install", failed_install, "service didn't load",
             codes={"BOOTSTRAP": 1, "PORT": 1, "SERVICE": 1},
             gap="Failed service startup still exits zero."),
        case("install_rollback_failure", "install", failed_install, "proxy disarmed (traffic direct",
             codes={"BOOTSTRAP": 1, "PORT": 1, "SERVICE": 1, "DISARM": 1},
             gap="Failed startup and failed rollback still exit zero and claim direct traffic."),
        case("install_port_without_service", "install",
             INSTALL_PREFIX + ["port", "launchctl:print", "disarm"] + INSTALL_SUFFIX,
             "service didn't load", codes={"SERVICE": 1},
             gap="Port-only liveness is correctly rejected, but the failed install exits zero."),
        case("install_plist_write_failure", "install", successful_install, "loaded, capture live",
             codes={"PLIST": 1},
             gap="Plist write failure does not prevent bootstrap or a success claim if later checks pass."),
        case("install_setup_instructions", "install", successful_install, "trust the cert",
             codes={"PERMISSION": 1, "ARMED": 1, "CERTIFICATE": 1}),
    ]


def run_case(item, script, root):
    event_file = root / "events"
    event_file.write_text("")
    # Do not inherit shell startup hooks or exported functions. Keep HOME as-is.
    env = {"HOME": os.environ["HOME"], "PATH": "/usr/bin:/bin", "LC_ALL": "C",
           "TAP_FIXTURE_ROOT": str(root), "TAP_FIXTURE_EVENTS": str(event_file)}
    env.update({f"TAP_FIXTURE_{key}": str(value) for key, value in item["codes"].items()})
    result = subprocess.run(["/bin/bash", "--noprofile", "--norc", str(script), item["command"]],
                            env=env, capture_output=True, text=True, timeout=10)
    observed = event_file.read_text().splitlines()
    errors = []
    if result.returncode != item["exit_code"]:
        errors.append(f"exit {result.returncode}, expected {item['exit_code']}")
    if observed != item["events"]:
        errors.append(f"events {observed!r}, expected {item['events']!r}")
    if item["text"] not in result.stdout:
        errors.append(f"missing output marker: {item['text']!r}")
    if result.stderr:
        errors.append(f"unexpected stderr: {result.stderr}")
    if errors:
        raise RuntimeError(f"{item['name']}: " + "; ".join(errors))
    return {"name": item["name"], "status": "known_gap" if item["gap"] else "verified_fixture",
            "exit_code": result.returncode, "events": observed,
            "observed_output_marker": item["text"], **({"gap": item["gap"]} if item["gap"] else {})}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path, help="Trusted legacy source directory")
    parser.add_argument("--output", type=Path, help="Optional JSON report destination")
    args = parser.parse_args()
    original = (args.source / "tap").read_bytes()
    digest = hashlib.sha256(original).hexdigest()
    if digest != SOURCE_SHA256:
        parser.error(f"unreviewed source SHA-256 {digest}; inspect reachable operations before updating the pin")
    source = original.decode("utf-8")
    if source.count(DISPATCH) != 1:
        parser.error("expected exactly one command dispatcher")
    prefix, dispatch = source.split(DISPATCH)
    with tempfile.TemporaryDirectory(prefix="tap-cli-fixture-") as directory:
        root = Path(directory)
        script = root / "tap-fixture"
        script.write_text(prefix + ADAPTERS + DISPATCH + dispatch)
        (root / "sudoers.snippet").write_text("# Synthetic display-only permission fixture\nfixture DISPLAY_ONLY\n")
        results = [run_case(item, script, root) for item in scenarios()]
    report = {"source_file": "tap", "source_sha256": digest,
              "scope": "Original on/off/install dispatch; lifecycle helpers and OS operations substituted",
              "live_integration": "not_tested", "cases": results,
              "matched_cases": len(results),
              "known_gap_cases": sum(item["status"] == "known_gap" for item in results)}
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered)
    print(rendered, end="")


if __name__ == "__main__":
    main()
