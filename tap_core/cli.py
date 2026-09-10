"""Local capture, page control, packs and diagnostics for this TAP installation."""
import argparse
from contextlib import nullcontext
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import shlex
import sys
import time

from .runtime import Lifecycle, MacOS, Profile, TapError, command_execution_lock, profile_lock
from .routing import select_routing, SystemProxyRouting

OFF_CLEANUP_WAIT_SECONDS = 15


def install_root_for_profile(profile):
    """Installer layout places the profile at <root>/profile beside install.json."""
    candidate = profile.root.parent
    if (candidate / "install.json").is_file():
        return candidate
    return None


def finish_setup_command(install_root):
    path = install_root / "checkout" / "instll" / "finish-setup"
    if not path.is_file():
        return None
    return f"bash {shlex.quote(str(path.resolve()))}"


def wrapper_bin_dir(install_root):
    try:
        data = json.loads((install_root / "install.json").read_text())
    except (OSError, ValueError):
        return None
    wrapper = data.get("wrapper")
    if not wrapper:
        return None
    return Path(wrapper).expanduser().resolve().parent


def path_export_command(install_root):
    bin_dir = wrapper_bin_dir(install_root)
    if bin_dir is None:
        return None
    return f"export PATH={shlex.quote(str(bin_dir))}:$PATH"


def path_needs_export(install_root):
    bin_dir = wrapper_bin_dir(install_root)
    if bin_dir is None:
        return False
    return str(bin_dir) not in os.environ.get("PATH", "").split(os.pathsep)


def _cert_fingerprint(path):
    text = Path(path).read_text()
    match = re.search(r"-----BEGIN CERTIFICATE-----(.*?)-----END CERTIFICATE-----", text, re.S)
    if not match:
        return None
    import base64
    der = base64.b64decode("".join(match.group(1).split()))
    return hashlib.sha256(der).hexdigest()


def ca_grant_recorded(install_root, profile):
    """True when finish-setup recorded a CA grant matching this profile certificate."""
    cert = profile.root / "certificates" / "mitmproxy-ca-cert.pem"
    if not cert.is_file():
        return False
    try:
        data = json.loads((install_root / "install.json").read_text())
    except (OSError, ValueError):
        return False
    recorded = ((data.get("grants") or {}).get("ca") or {}).get("sha256") or ""
    actual = _cert_fingerprint(cert)
    return bool(recorded and actual and recorded.lower() == actual.lower())


def finish_setup_next(profile):
    """Copy-paste finish-setup when this install still needs sudoers/CA trust."""
    install_root = install_root_for_profile(profile)
    if install_root is None:
        return None
    command = finish_setup_command(install_root)
    if command is None:
        return None
    if ca_grant_recorded(install_root, profile):
        return None
    return command


def next_shell_commands(profile, *, setup=None):
    """Ordered copy-paste lines for stderr / doctor["next"]."""
    install_root = install_root_for_profile(profile)
    if install_root is None:
        return []
    if setup is None:
        setup = finish_setup_next(profile)
    lines = []
    export = path_export_command(install_root)
    # Always pair PATH with finish-setup (fresh shell copy-paste); else only if missing.
    if export and (setup or path_needs_export(install_root)):
        lines.append(export)
    if setup:
        lines.append(setup)
    return lines


def print_next_commands(profile, *, setup=None):
    lines = next_shell_commands(profile, setup=setup)
    for line in lines:
        print(f"tap-core: next: {line}", file=sys.stderr)
    return lines


def print_finish_setup_next(profile):
    return print_next_commands(profile)


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
    from .bridge import fingerprint
    from .pack_store import PackStore
    effective = PackStore(profile.root).effective_bridge(profile.bridge)
    if effective is None:
        return {"configured": False, "healthy": True}
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
               and state['configuration'] == fingerprint(effective)
               and state['enabled'] is effective['enabled'])
    return {"configured": True, "healthy": healthy, "enabled": effective['enabled'],
            "hub_port": effective['hub_port'], "applies": "startup snapshot",
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
    from .background import status as background_status
    result["background"] = observe("background", lambda: background_status(profile.root))
    result["inspection_errors"] = errors
    return result


def doctor(profile, adapter):
    result = status(profile, adapter)
    try:
        result["backend_version"] = adapter.backend_version(profile)
    except TapError as error:
        result["backend_error"] = str(error)
    result["ca_file_present"] = (profile.root / "certificates/mitmproxy-ca-cert.pem").is_file()
    install_root = install_root_for_profile(profile)
    setup = finish_setup_command(install_root) if install_root is not None else None
    ca_granted = bool(install_root and ca_grant_recorded(install_root, profile))
    if ca_granted:
        result["ca_trust"] = "granted; finish-setup recorded System keychain trust for this profile CA"
    else:
        result["ca_trust"] = "not_verified; HTTPS clients must trust this profile CA explicitly"
    if profile.routing == "system":
        listed = adapter.run(["/usr/bin/sudo", "-n", "-l"], check=False).stdout
        ready = "TAP_CORE_PROXY" in listed or "networksetup -setwebproxy" in listed
        result["sudoers"] = {"ready": ready, "next": None if ready else setup}
        if not ready:
            result["inspection_errors"]["sudoers"] = (
                "system routing needs finish-setup (sudoers + CA trust); "
                "run the finish-setup from this installation's checkout")
    need_setup = bool(setup and (not ca_granted or (profile.routing == "system"
                                                     and not result.get("sudoers", {}).get("ready"))))
    next_lines = next_shell_commands(profile, setup=setup if need_setup else None)
    if next_lines:
        result["next"] = next_lines
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
    # Installed launchers supply their owned profile. Keep the option for
    # isolated fixtures and advanced multi-instance use without presenting it
    # as part of the ordinary command surface.
    result.add_argument("--profile", type=Path, required=True, help=argparse.SUPPRESS)
    commands = result.add_subparsers(dest="command", required=True)
    install = commands.add_parser(
        "install", help="Install or repair this TAP instance; do not arm system proxy")
    install.add_argument("--backend", type=Path, help="mitmdump 12.2.3 executable (required for first setup)")
    install.add_argument("--port", type=int, help="proxy port (required for first setup)")
    install.add_argument("--routing", choices=["explicit", "system"], help="routing mode (required for first setup)")
    install.add_argument("--probe-url")
    install.add_argument("--addon", type=Path, action="append", help="Additional trusted addon (optional)")
    install.add_argument("--bridge-config", type=Path, help="Explicit page bridge configuration JSON")
    install.add_argument("--components-config", type=Path, help="Explicit managed Hub/reader/handler development bindings")
    for name in ("on", "off", "doctor", "where", "uninstall"):
        commands.add_parser(name)
    status_command = commands.add_parser("status")
    status_command.add_argument("--output", choices=("raw-json", "semantic-json", "terminal"),
                                default="raw-json")
    status_command.add_argument("--width", type=int, default=80)
    status_command.add_argument("--color", choices=("auto", "always", "never"), default="auto")
    routing = commands.add_parser(
        "routing",
        help="Switch this installation's routing (explicit/system) in place; install/uninstall never change routing")
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
    pack = commands.add_parser("pack", help="Install and activate external pack artifacts")
    pack_actions = pack.add_subparsers(dest="pack_action", required=True)
    pack_actions.add_parser("list")
    pack_add = pack_actions.add_parser("add", help="Download, verify, consent to and enable a GitHub release")
    pack_add.add_argument("source", help="OWNER/REPO or OWNER/REPO@MAJOR.MINOR.PATCH")
    pack_add.add_argument("--yes", action="store_true", help="Accept the displayed access request non-interactively")
    pack_install = pack_actions.add_parser("install")
    pack_install.add_argument("artifact", type=Path)
    pack_update = pack_actions.add_parser("update")
    pack_update.add_argument("artifact", type=Path)
    pack_enable = pack_actions.add_parser("enable")
    pack_enable.add_argument("id")
    pack_enable.add_argument("--version", required=True)
    pack_enable.add_argument("--grant-origin", action="append", default=[])
    pack_enable.add_argument("--grant-capability", action="append", default=[])
    pack_enable.add_argument("--dependency", action="append", default=[])
    pack_enable.add_argument("--config", type=Path)
    for action in ("rollback", "disable", "uninstall"):
        command = pack_actions.add_parser(action)
        command.add_argument("id")
        if action == "uninstall":
            command.add_argument("--version")
    commands.add_parser("pages", help="List live pages connected to the local WebSocket bridge")
    page = commands.add_parser("page", help="Send an opaque command to an operation exposed by a page pack")
    page_actions = page.add_subparsers(dest="page_action", required=True)
    page_call = page_actions.add_parser("call")
    page_call.add_argument("page_id")
    page_call.add_argument("operation")
    page_call.add_argument("--args", default="{}", help="JSON value passed unchanged to the page operation")
    dev = commands.add_parser("dev", help="Inspect and change one live page through the local development channel")
    dev_actions = dev.add_subparsers(dest="dev_action", required=True)
    dev_actions.add_parser("pages", help="List pages currently connected to the development channel")
    dev_inspect = dev_actions.add_parser("inspect", help="Read a bounded DOM projection from one live page")
    dev_inspect.add_argument("page_id")
    dev_inspect.add_argument("selector")
    dev_inspect.add_argument("--limit", type=int, default=20)
    dev_execute = dev_actions.add_parser("execute", help="Run explicit development JavaScript in one live page")
    dev_execute.add_argument("page_id")
    dev_source = dev_execute.add_mutually_exclusive_group(required=True)
    dev_source.add_argument("--source", help="JavaScript function body; use return to produce a result")
    dev_source.add_argument("--file", type=Path, help="Read the JavaScript function body from this local file")
    return result


def _profile_and_words(argv):
    """Read only the global profile prefix; provider argv remains untouched."""
    profile = None
    index = 0
    while index < len(argv):
        value = argv[index]
        if value == "--profile":
            if index + 1 >= len(argv):
                return None, argv[index:]
            profile = Path(argv[index + 1])
            index += 2
            continue
        if value.startswith("--profile="):
            profile = Path(value.partition("=")[2])
            index += 1
            continue
        break
    return profile, argv[index:]


def _early_command_dispatch(argv):
    from .commands import CORE_ROOTS, discover, dispatch, render_help
    profile, words = _profile_and_words(argv)
    if words in (["--help"], ["-h"]):
        parser().print_help()
        print()
        render_help(discover(profile.expanduser().resolve() if profile else None))
        return 0
    if not words or words[0] in CORE_ROOTS - {"where"}:
        return None
    if profile is None:
        return None
    return dispatch(profile, words)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        early = _early_command_dispatch(argv)
    except (TapError, OSError, ValueError) as error:
        print(f"tap: {error}", file=sys.stderr)
        return 1
    if early is not None:
        return early
    args = parser().parse_args(argv)
    if platform.system() != "Darwin":
        print("This extraction currently supports macOS only", file=sys.stderr)
        return 1
    root = args.profile.expanduser().resolve()
    adapter = MacOS()
    try:
        if args.command == 'pack':
            from .components import Job
            from .pack_store import LIVE_PACK_APPLIES, PackStore, _parse_dependency
            store = PackStore(root)
            profile = Profile.load(root) if (root / "profile.json").is_file() else None
            if args.pack_action == 'list':
                output = store.status()
            else:
                # Do not change or remove a selected pack while its immutable
                # command snapshot is executing. This execution lease is
                # independent of capture/network lifecycle.
                with command_execution_lock(
                        root,
                        busy_message="A command from this profile is still running; "
                                     "wait before changing packs"), profile_lock(root):
                    # Reader/handler projection is refused before registry publish so a
                    # concurrent bridge refresh cannot observe a rejected plan.
                    live = (
                        profile is not None
                        and (adapter.service_loaded(profile)
                             or (profile.components is not None and adapter.service_loaded(Job(profile)))))
                    if args.pack_action == 'add':
                        from .pack_add import add
                        output = add(root, args.source, assume_yes=args.yes, live=live)
                    elif args.pack_action == 'install':
                        output = store.install(args.artifact)
                    elif args.pack_action == 'update':
                        output = store.update(args.artifact, live=live)
                    elif args.pack_action == 'enable':
                        from .bridge import read_json
                        config = read_json(args.config) if args.config else None
                        output = store.enable(args.id, args.version,
                                              origins=args.grant_origin,
                                              capabilities=args.grant_capability,
                                              dependencies=_parse_dependency(args.dependency),
                                              config=config, live=live)
                    elif args.pack_action == 'rollback':
                        output = store.rollback(args.id, live=live)
                    elif args.pack_action == 'disable':
                        output = store.disable(args.id, live=live)
                    else:
                        output = store.uninstall(args.id, args.version)
                    from .background import reconcile
                    try:
                        reconcile(root, adapter)
                    except (TapError, OSError) as error:
                        raise TapError(f"Pack state saved, but background registration failed: {error}; retry pack enable/disable to reconcile") from error
                    if isinstance(output, dict) and 'applies' in output and live:
                        output = dict(output)
                        output['applies'] = LIVE_PACK_APPLIES
            print(json.dumps(output, indent=2))
            return 0
        repair_install = args.command == "install" and (root / "profile.json").is_file()
        if args.command == "install":
            if repair_install:
                supplied = [name for name in ("backend", "port", "routing", "probe_url",
                                               "addon", "bridge_config", "components_config")
                            if getattr(args, name) not in (None, [])]
                if supplied:
                    raise TapError("Existing installation repair uses its saved configuration; "
                                   "remove install options: " + ", ".join(supplied))
                profile = Profile.load(root)
            else:
                missing = [name for name in ("backend", "port", "routing") if getattr(args, name) is None]
                if missing:
                    raise TapError("New profile install requires: " + ", ".join("--" + name for name in missing))
                profile = Profile(root, str(args.backend.expanduser().resolve()), args.port, args.routing,
                                  args.probe_url or "http://example.com/",
                                  [str(p.expanduser().resolve()) for p in (args.addon or [])])
                if args.bridge_config:
                    from .bridge import configuration, read_json
                    profile.bridge = configuration(read_json(args.bridge_config))
                if args.components_config:
                    from .components import configuration
                    from .bridge import read_json
                    profile.components = configuration(read_json(args.components_config), profile)
        else:
            profile = Profile.load(root)
        if args.command == "pages":
            from .page_control import pages
            print(json.dumps(pages(root), indent=2))
            return 0
        if args.command == "page":
            from .page_control import call
            try:
                page_args = json.loads(args.args)
            except ValueError as error:
                raise TapError(f"Invalid --args JSON: {error}") from error
            print(json.dumps(call(root, args.page_id, args.operation, page_args), indent=2))
            return 0
        if args.command == "dev":
            from .page_control import execute, inspect, pages
            if args.dev_action == "pages":
                output = pages(root)
            elif args.dev_action == "inspect":
                if not 1 <= args.limit <= 100:
                    raise TapError("Development inspection limit must be between 1 and 100")
                output = inspect(root, args.page_id, args.selector, args.limit)
            else:
                try:
                    source = args.file.expanduser().resolve().read_text() if args.file else args.source
                except OSError as error:
                    raise TapError(f"Cannot read development source: {error}") from error
                if not source or len(source.encode()) > 65536:
                    raise TapError("Development source must contain 1 to 65536 UTF-8 bytes")
                output = execute(root, args.page_id, source)
            print(json.dumps(output, indent=2))
            return 0
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
                from .pack_store import PackStore
                effective = PackStore(root).effective_bridge(configuration(profile.bridge))
                output = decision(effective, args.origin)
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
        if args.command == "status":
            result = status(profile, adapter)
            if args.output == "raw-json":
                print(json.dumps(result, indent=2))
            else:
                # Keep the compatibility path independent of optional presentation.
                from .status_view import public_status_result, terminal_status
                semantic = public_status_result(result)
                if args.output == "semantic-json":
                    print(json.dumps(semantic, indent=2))
                else:
                    if args.width < 1:
                        raise TapError("Status width must be a positive integer")
                    color = args.color == "always" or (args.color == "auto" and sys.stdout.isatty())
                    print(terminal_status(semantic, args.width, color))
            return 0
        if args.command == "doctor":
            result = doctor(profile, adapter)
            print(json.dumps(result, indent=2))
            for line in result.get("next") or []:
                print(f"tap-core: next: {line}", file=sys.stderr)
            return 1 if not result["healthy"] else 0
        if args.command == "routing":
            with profile_lock(root):
                # Reload under the lock: the profile read before locking may be
                # stale if another command changed routing in between, which would
                # otherwise decide the shared lock or the no-op on old state.
                profile = Profile.load(root)
                # Leaving OR entering system mutates shared network settings, so
                # take the shared network lock whenever either side is system —
                # not only when the current mode is system (that would miss the
                # explicit -> system direction).
                needs_network_lock = profile.routing == "system" or args.mode == "system"
                with (SystemProxyRouting(profile, adapter).mutation_lock()
                      if needs_network_lock else nullcontext()):
                    output = routing_set(profile, adapter, args.mode)
            print(output)
            return 0
        # `off` is the operator's network escape hatch. A periodic pack command
        # may hold the ordinary profile lease for its whole child lifetime; do
        # not make proxy recovery wait behind that unrelated work. The recovery
        # snapshot is the ownership journal, and the shared network lease still
        # serializes system settings across profiles. Cleanup then waits for a
        # bounded drain and repeats restore under the normal lock ordering.
        network_recovered = False
        if args.command == "off" and (profile.routing == "system" or profile.snapshot.exists()):
            emergency_route = SystemProxyRouting(profile, adapter)
            with emergency_route.mutation_lock():
                emergency_route.restore()
            network_recovered = True

        # Serialize system-routing commands across profiles as well as per-profile.
        with profile_lock(
                root,
                wait_seconds=OFF_CLEANUP_WAIT_SECONDS if network_recovered else 0,
                busy_message=(
                    "Network restored; profile cleanup is still pending because a command "
                    "did not finish within the bounded drain"
                    if network_recovered else "Another command is changing this profile")):
            if args.command != "install":
                # Reload under the lock. routing selects both the shared network
                # lock and the restore/enable behavior, so on/off/uninstall must
                # act on the saved mode — not a copy read before locking, which a
                # concurrent `routing set` could have changed (e.g. off would then
                # skip the network lock and no-op the restore, leaving the system
                # proxy armed at a stopped service). install builds a new profile
                # and keeps its own semantics.
                profile = Profile.load(root)
            with select_routing(profile, adapter).mutation_lock():
                if args.command == "install":
                    output = mutate(args.command, profile, adapter, repair_install=repair_install)
                else:
                    output = mutate(args.command, profile, adapter)
        print(output)
        if args.command == "on":
            print_finish_setup_next(profile)
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
        # The mode is already committed; on() is a separate step. If it fails,
        # say so plainly instead of letting on()'s "settings were not changed"
        # message hide that the profile is now in the new mode, stopped.
        try:
            return f"Routing set to {target}; {lifecycle.on()}"
        except (TapError, OSError) as error:
            raise TapError(f"Routing set to {target}, but starting it failed: {error}. "
                           f"The saved routing is now {target}; startup is incomplete and services may still be running. "
                           "Inspect status/doctor; use off to recover before retrying on.") from error
    return f"Routing set to {target}; profile remains stopped — run on to start it"


def _reconcile_background(root, adapter):
    """Keep optional scheduled pack commands outside capture recovery."""
    from .background import reconcile
    try:
        reconcile(root, adapter)
    except (TapError, OSError, ValueError) as error:
        return f"; background commands unavailable: {error}"
    return ""


def mutate(command, profile, adapter, *, repair_install=False):
    lifecycle = Lifecycle(profile, adapter)
    if command == "install":
        result = lifecycle.install(repair=repair_install)
        return result + _reconcile_background(profile.root, adapter)
    if command == "uninstall":
        lifecycle.off()
        from .background import reconcile
        reconcile(profile.root, adapter, remove=True)
        profile.plist.unlink(missing_ok=True)
        return "Profile service removed; configuration, captured data and certificates retained"
    result = getattr(lifecycle, command)()
    if command == "on":
        result += _reconcile_background(profile.root, adapter)
    return result
