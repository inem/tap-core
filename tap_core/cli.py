"""Six existing TAP commands, with profile-scoped configuration and diagnostics."""
import argparse
import json
import os
from pathlib import Path
import platform
import sys
import time

from .runtime import Lifecycle, MacOS, Profile, TapError, profile_lock


def health(profile, adapter):
    try:
        state = json.loads((profile.root / "state/capture.json").read_text())
    except (OSError, ValueError):
        return {"available": False, "healthy": False}
    if (not isinstance(state, dict) or not isinstance(state.get("updated_at"), (int, float))
            or type(state.get("pid")) is not int or state["pid"] <= 0):
        raise TapError("Invalid capture health record")
    current = state.get("pid") == adapter.service_pid(profile) and 0 <= time.time() - state["updated_at"] < 5
    return {**state, "current_process": current,
            "healthy": current and state.get("writer_alive", False) and state.get("write_errors") == 0}


def status(profile, adapter):
    errors = {}
    def observe(name, operation, unavailable=None):
        try:
            return operation()
        except (TapError, OSError, ValueError) as error:
            errors[name] = str(error)
            return unavailable
    result = {"profile": str(profile.root), "routing": profile.routing, "port": profile.port,
              "service_loaded": observe("service_loaded", lambda: adapter.service_loaded(profile)),
              "pid": observe("pid", lambda: adapter.service_pid(profile)),
              "port_owned": observe("port_owned", lambda: adapter.owns_port(profile)),
              "port_open": observe("port_open", lambda: adapter.port_open(profile)),
              "network_recovery_pending": observe("network_recovery_pending", profile.snapshot.exists),
              "system_proxy_verified": observe("system_proxy_verified", lambda: adapter.armed(profile)) if profile.routing == "system" else "not_used",
              "capture": observe("capture", lambda: health(profile, adapter), {"available": False, "healthy": False})}
    result["inspection_errors"] = errors
    return result


def doctor(profile, adapter):
    result = status(profile, adapter)
    try:
        result["backend_version"] = adapter.backend_version(profile)
    except TapError as error:
        result["backend_error"] = str(error)
    result["ca_file_present"] = (profile.root / "certificates/mitmproxy-ca-cert.pem").is_file()
    result["ca_trust"] = "not_verified; HTTPS clients must trust this profile CA explicitly"
    result["traffic_probe"] = None
    if result["port_owned"] is True:
        try:
            result["traffic_probe"] = adapter.flows(profile)
        except TapError as error:
            result["inspection_errors"]["traffic_probe"] = str(error)
    result["healthy"] = bool("backend_error" not in result and not result["inspection_errors"] and result["port_owned"]
                         and result["capture"]["healthy"] and result["traffic_probe"]
                         and (profile.routing == "explicit" or result["system_proxy_verified"]))
    return result


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--profile", type=Path, required=True, help="Explicit profile directory")
    commands = result.add_subparsers(dest="command", required=True)
    install = commands.add_parser("install", help="Install and start this profile through launchd; do not arm system proxy")
    install.add_argument("--backend", type=Path, required=True, help="mitmdump 12.2.3 executable")
    install.add_argument("--port", type=int, required=True)
    install.add_argument("--routing", choices=["explicit", "system"], required=True)
    install.add_argument("--probe-url", default="http://example.com/")
    install.add_argument("--addon", type=Path, action="append", default=[], help="Additional trusted addon (optional)")
    for name in ("on", "off", "status", "doctor", "where", "uninstall"):
        commands.add_parser(name)
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    if platform.system() != "Darwin":
        print("This extraction currently supports macOS only", file=sys.stderr)
        return 1
    root = args.profile.expanduser().resolve()
    adapter = MacOS()
    try:
        if args.command == "install":
            profile = Profile(root, str(args.backend.expanduser().resolve()), args.port, args.routing,
                              args.probe_url, [str(p.expanduser().resolve()) for p in args.addon])
        else:
            profile = Profile.load(root)
        if args.command in ("status", "doctor", "where"):
            if args.command == "where":
                result = {"profile": str(root), "config": str(root / "profile.json"),
                          "data": str(root / "data"), "state": str(root / "state"),
                          "certificates": str(root / "certificates"), "log": str(root / "logs/capture.log"),
                          "launch_agent": str(profile.plist), "backend": profile.backend,
                          "checkout": str(Path(__file__).resolve().parent.parent)}
            else:
                result = doctor(profile, adapter) if args.command == "doctor" else status(profile, adapter)
            print(json.dumps(result, indent=2))
            return 1 if args.command == "doctor" and not result["healthy"] else 0
        # Serialize system-routing commands across profiles as well as per-profile.
        with profile_lock(root):
            if profile.routing == "system":
                shared = Path.home() / "Library/Application Support/TAP Core/network-control"
                with profile_lock(shared):
                    output = mutate(args.command, profile, adapter)
            else:
                output = mutate(args.command, profile, adapter)
        print(output)
        return 0
    except (TapError, OSError, ValueError) as error:
        print(f"tap: {error}", file=sys.stderr)
        return 1


def mutate(command, profile, adapter):
    lifecycle = Lifecycle(profile, adapter)
    if command == "install":
        if (profile.root / "profile.json").exists():
            raise TapError("Profile already exists; use on, or choose a new directory")
        return lifecycle.install()
    if command == "uninstall":
        lifecycle.off()
        profile.plist.unlink(missing_ok=True)
        return "Profile service removed; configuration, captured data and certificates retained"
    return getattr(lifecycle, command)()
