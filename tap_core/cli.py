"""Six existing TAP commands, with profile-scoped configuration and diagnostics."""
import argparse
from contextlib import nullcontext
import json
import math
import os
from pathlib import Path
import platform
import re
import sys
import time

from .runtime import Lifecycle, MacOS, Profile, TapError, profile_lock
from .routing import select_routing, SystemProxyRouting


def health(profile, adapter):
    try:
        state = json.loads((profile.root / "state/capture.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"available": False, "healthy": False, "current_process": False}
    except OSError as error:
        raise TapError(f"Cannot read capture health record: {error}") from error
    except ValueError as error:
        raise TapError(f"Invalid capture health JSON: {error}") from error
    if not isinstance(state, dict):
        raise TapError("Invalid capture health record: expected an object")
    for name in ("pid", "written", "dropped", "write_errors", "queued_bytes"):
        value = state.get(name)
        if type(value) is not int or value < (1 if name == "pid" else 0):
            raise TapError(f"Invalid capture health metric: {name}")
    timestamp = state.get("updated_at")
    if (type(timestamp) not in (int, float) or timestamp <= 0
            or (type(timestamp) is float and not math.isfinite(timestamp))):
        raise TapError("Invalid capture health metric: updated_at")
    if type(state.get("writer_alive")) is not bool:
        raise TapError("Invalid capture health metric: writer_alive")
    if "last_error" not in state or (state["last_error"] is not None and not isinstance(state["last_error"], str)):
        raise TapError("Invalid capture health metric: last_error")
    now = time.time()
    current = state["pid"] == adapter.service_pid(profile) and now - 5 < timestamp <= now
    return {**state, "available": True, "current_process": current,
            "healthy": current and state["writer_alive"] and state["write_errors"] == 0}


def bridge_status(profile, adapter):
    if profile.bridge is None:
        return {"configured": False, "healthy": True}
    from .bridge import fingerprint
    try:
        state = json.loads((profile.root / 'state/bridge.json').read_text())
        if state is None:
            raise TapError('Invalid bridge startup record')
    except FileNotFoundError:
        state = None
    if state is not None and (
            type(state) is not dict or set(state) != {'pid', 'configuration', 'enabled'}
            or type(state['pid']) is not int or state['pid'] < 1
            or type(state['enabled']) is not bool
            or not isinstance(state['configuration'], str)
            or not re.fullmatch('[0-9a-f]{64}', state['configuration'])):
        raise TapError('Invalid bridge startup record')
    healthy = (state is not None and state['pid'] == adapter.service_pid(profile)
               and state['configuration'] == fingerprint(profile.bridge)
               and state['enabled'] is profile.bridge['enabled'])
    return {"configured": True, "healthy": healthy, "enabled": profile.bridge['enabled'],
            "hub_port": profile.bridge['hub_port'], "applies": "startup snapshot",
            "hub_liveness": "not_checked"}


def status(profile, adapter):
    route = select_routing(profile, adapter)
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
              "network_recovery_pending": observe("network_recovery_pending", route.recovery_pending),
              "system_proxy_verified": observe("system_proxy_verified", route.verified),
              "routing_adapter": route.capabilities(),
              "capture": observe("capture", lambda: health(profile, adapter),
                                 {"available": None, "healthy": None, "current_process": None})}
    result['bridge'] = observe('bridge', lambda: bridge_status(profile, adapter),
                               {'configured': profile.bridge is not None, 'healthy': None})
    from .components import status as component_status
    result['components'] = observe('components', lambda: component_status(profile, adapter),
                                   {'configured': profile.components is not None, 'healthy': None})
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
            result["traffic_probe"] = select_routing(profile, adapter).probe()
        except TapError as error:
            result["inspection_errors"]["traffic_probe"] = str(error)
    result["healthy"] = bool("backend_error" not in result and not result["inspection_errors"] and result["port_owned"]
                         and result["capture"]["healthy"] and result["traffic_probe"] and result["bridge"]["healthy"]
                         and result['components']['healthy']
                         and (result["system_proxy_verified"] == "not_used" or result["system_proxy_verified"] is True))
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
    install.add_argument("--bridge-config", type=Path, help="Explicit page bridge configuration JSON")
    install.add_argument("--components-config", type=Path, help="Explicit managed Hub/reader/handler development bindings")
    for name in ("on", "off", "status", "doctor", "where", "uninstall"):
        commands.add_parser(name)
    routing = commands.add_parser(
        "routing",
        help="Switch this profile's routing (explicit/system) in place; install/uninstall never change routing")
    routing_actions = routing.add_subparsers(dest="routing_action", required=True)
    set_routing = routing_actions.add_parser(
        "set",
        help="Change routing to explicit or system without editing JSON, deleting the profile or reinstalling; "
             "preserves capture, reader checkpoints, certificates, tokens, bridge/component settings and grants. "
             "A running profile is returned to running, a stopped one stays stopped; managed components restart and "
             "open connections may drop.")
    set_routing.add_argument("mode", choices=["explicit", "system"])
    components = commands.add_parser('components', help='Configure explicit development component bindings')
    component_actions = components.add_subparsers(dest='component_action', required=True)
    configure_components = component_actions.add_parser('configure')
    configure_components.add_argument('--config', type=Path, required=True, help='JSON binding, or null to disable')
    bridge = commands.add_parser('bridge', help='Configure or explain page injection and local routes')
    bridge_actions = bridge.add_subparsers(dest='bridge_action', required=True)
    configure = bridge_actions.add_parser('configure')
    configure.add_argument('--config', type=Path, required=True)
    explain = bridge_actions.add_parser('explain')
    explain.add_argument('--origin', required=True)
    reader = commands.add_parser("reader", help="Run independent readers over retained capture")
    actions = reader.add_subparsers(dest="reader_action", required=True)
    for action in ("run", "status", "replay"):
        command = actions.add_parser(action)
        command.add_argument("name")
        if action != "status":
            command.add_argument("--definition", type=Path, required=True)
        if action == "run":
            command.add_argument("--max-records", type=int, default=100)
            command.add_argument("--timeout", type=float, default=30)
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
            if args.bridge_config:
                from .bridge import configuration, read_json
                profile.bridge = configuration(read_json(args.bridge_config))
            if args.components_config:
                from .components import configuration
                from .bridge import read_json
                profile.components = configuration(read_json(args.components_config), profile)
        else:
            profile = Profile.load(root)
        if args.command == 'components':
            from .components import configuration, Job
            from .bridge import read_json
            with profile_lock(root):
                if adapter.service_loaded(profile) or adapter.service_loaded(Job(profile)):
                    raise TapError('Stop this profile with off before changing component configuration')
                value = read_json(args.config)
                profile.components = None if value is None else configuration(value, profile)
                profile.save()
            print(json.dumps({'configured': value is not None, 'applies': 'next on'}, indent=2))
            return 0
        if args.command == 'bridge':
            from .bridge import configuration, read_json, decision
            if args.bridge_action == 'configure':
                with profile_lock(root):
                    from .components import Job
                    if adapter.service_loaded(profile) or (profile.components is not None and adapter.service_loaded(Job(profile))):
                        raise TapError('Stop this profile with off before changing bridge configuration')
                    profile.bridge = configuration(read_json(args.config))
                    profile.save()
                output = {'configured': True, 'applies': 'next on'}
            else:
                if profile.bridge is None:
                    raise TapError('No bridge configured in this profile')
                output = decision(configuration(profile.bridge), args.origin)
            print(json.dumps(output, indent=2))
            return 0
        if args.command == "reader":
            from .readers import Reader, definition
            reader = Reader(profile, args.name)
            if args.reader_action == "status":
                output = reader.status()
            elif args.reader_action == "replay":
                output = reader.replay(definition(args.definition))
            else:
                output = reader.run(definition(args.definition), args.max_records, args.timeout)
            print(json.dumps(output, indent=2))
            return 0
        if args.command in ("status", "doctor", "where"):
            if args.command == "where":
                result = {"profile": str(root), "config": str(root / "profile.json"),
                          "data": str(root / "data"), "state": str(root / "state"),
                          "certificates": str(root / "certificates"), "log": str(root / "logs/capture.log"),
                          "launch_agent": str(profile.plist), "backend": profile.backend,
                          "checkout": str(Path(__file__).resolve().parent.parent)}
                if profile.components is not None:
                    from .components import Job
                    result['component_launch_agent'] = str(Job(profile).plist)
                    result['component_log'] = str(root / 'logs/components.log')
                    result['handler_logs'] = str(root / 'logs/handlers')
            else:
                result = doctor(profile, adapter) if args.command == "doctor" else status(profile, adapter)
            print(json.dumps(result, indent=2))
            return 1 if args.command == "doctor" and not result["healthy"] else 0
        if args.command == "routing":
            # Leaving OR entering system mutates shared network settings, so take
            # the shared network lock whenever either side is system — not only
            # when the current mode is system (that would miss explicit -> system).
            needs_network_lock = profile.routing == "system" or args.mode == "system"
            with profile_lock(root):
                with (SystemProxyRouting(profile, adapter).mutation_lock()
                      if needs_network_lock else nullcontext()):
                    output = routing_set(profile, adapter, args.mode)
            print(output)
            return 0
        # Serialize system-routing commands across profiles as well as per-profile.
        with profile_lock(root):
            with select_routing(profile, adapter).mutation_lock():
                output = mutate(args.command, profile, adapter)
        print(output)
        return 0
    except (TapError, OSError, ValueError) as error:
        print(f"tap: {error}", file=sys.stderr)
        return 1


def routing_set(profile, adapter, target):
    """Switch an existing profile between explicit and system routing in place.

    Safety order: the OLD routing releases the network (restore) BEFORE the new
    mode is committed, so a failed recovery leaves both the network and the saved
    mode untouched — no silent success. Running state is preserved: a running
    profile is stopped under its old routing and restarted under the new one; a
    stopped one is only reconfigured. The caller holds the profile lock and, when
    either side is system, the shared network lock.
    """
    if target not in ("explicit", "system"):
        raise TapError(f"Unsupported routing mode: {target}; no change was made")
    if profile.routing == target:
        # Idempotent: never duplicate services or overwrite the recovery snapshot.
        return f"Routing already {target} for this profile; nothing changed"
    lifecycle = Lifecycle(profile, adapter)
    running = adapter.service_loaded(profile)
    if running:
        # off() restores the network via the OLD routing and stops the service;
        # if recovery fails it raises here, before the new mode is committed.
        lifecycle.off()
    else:
        old_route = select_routing(profile, adapter)
        if old_route.recovery_pending():
            # Stopped but the old routing left a snapshot to restore first.
            old_route.restore()
    profile.routing = target
    profile.save()
    if running:
        return f"Routing set to {target}; {lifecycle.on()}"
    return f"Routing set to {target}; profile remains stopped — run on to start it"


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
