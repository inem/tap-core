"""Declarative command registry and argv subprocess host for installed packs."""

from dataclasses import dataclass
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

from .packs import PackError, check_activation, resolve_config
from .runtime import Profile, TapError, profile_lock


COMMAND_API = 1
CORE_PROVIDER_VERSION = "development"
CORE_ROOTS = frozenset({
    "install", "on", "off", "status", "doctor", "where", "uninstall",
    "routing", "components", "bridge", "reader", "pack",
})


@dataclass(frozen=True)
class Command:
    path: tuple
    summary: str
    usage: str
    profile: str
    provider_id: str
    provider_version: str
    source: str
    root: Path = None
    file: str = None
    runtime: str = None
    config: dict = None
    grants: dict = None

    @property
    def label(self):
        return " ".join(self.path)


@dataclass
class Registry:
    commands: dict
    diagnostics: list

    def match(self, words):
        matches = [command for path, command in self.commands.items()
                   if tuple(words[:len(path)]) == path]
        return max(matches, key=lambda command: len(command.path), default=None)

    def children(self, prefix=()):
        prefix = tuple(prefix)
        return [command for path, command in sorted(self.commands.items())
                if path[:len(prefix)] == prefix]


class _CommandParser(argparse.ArgumentParser):
    def error(self, message):
        raise TapError(message)


def _where_options(argv):
    parser = _CommandParser(prog="tap where", add_help=False, allow_abbrev=False)
    parser.add_argument("--output", choices=("raw-json", "semantic-json", "terminal"),
                        default="raw-json")
    parser.add_argument("--width", type=int)
    parser.add_argument("--color", choices=("auto", "always", "never"), default="auto")
    return parser.parse_args(argv)


def _where_result(profile_root, profile):
    return {
        "profile": str(profile_root), "config": str(profile_root / "profile.json"),
        "data": str(profile_root / "data"), "state": str(profile_root / "state"),
        "certificates": str(profile_root / "certificates"),
        "log": str(profile_root / "logs/capture.log"),
        "packs": str(profile_root / "packs"),
        "resources": str(profile_root / "resources"),
        "pack_registry": str(profile_root / "state/pack-registry.json"),
        "launch_agent": str(profile.plist), "backend": profile.backend,
        "checkout": str(Path(__file__).resolve().parent.parent),
    }


def _where(profile_root, argv):
    options = _where_options(argv)
    profile = Profile.load(profile_root)
    result = _where_result(profile_root, profile)
    if profile.components is not None:
        from .components import Job
        result["component_launch_agent"] = str(Job(profile).plist)
        result["component_log"] = str(profile_root / "logs/components.log")
        result["handler_logs"] = str(profile_root / "logs/handlers")
    if options.output == "raw-json":
        print(json.dumps(result, indent=2))
        return 0
    from .where_view import public_where_result, terminal_where
    semantic = public_where_result(result)
    if options.output == "semantic-json":
        print(json.dumps(semantic, indent=2))
    else:
        if options.width is not None and options.width < 1:
            raise TapError("Where width must be a positive integer")
        color = options.color == "always" or (options.color == "auto" and sys.stdout.isatty())
        print(terminal_where(semantic, options.width, color))
    return 0


def builtin_commands():
    command = Command(
        path=("where",), summary="Show profile-owned paths and runtime locations",
        usage="[--output raw-json|semantic-json|terminal] [--width N] [--color auto|always|never]",
        profile="required", provider_id="tap-core",
        provider_version=CORE_PROVIDER_VERSION, source="builtin",
    )
    return {command.path: command}


def _paths_conflict(left, right):
    size = min(len(left), len(right))
    return left[:size] == right[:size]


def validate_external_command_paths(manifests):
    """Reject exact and leaf/prefix ambiguity before an activation is committed."""
    owners = []
    for pack_id, manifest in manifests:
        entry = manifest["entrypoints"].get("command")
        if entry is None:
            continue
        for declaration in entry["commands"]:
            path = tuple(declaration["path"])
            if path[0] in CORE_ROOTS:
                raise PackError(
                    f"command {' '.join(path)} from {pack_id} conflicts with built-in root {path[0]}")
            for other_path, other_pack in owners:
                if _paths_conflict(path, other_path):
                    raise PackError(
                        f"command {' '.join(path)} from {pack_id} conflicts with "
                        f"{' '.join(other_path)} from {other_pack}")
            owners.append((path, pack_id))


def _pack_commands(profile_root):
    from .pack_store import PackStore
    store = PackStore(profile_root)
    commands = []
    diagnostics = []
    try:
        registry = store.load()
    except (PackError, OSError, ValueError) as error:
        return [], [f"pack registry unavailable: {error}"]
    for pack_id in sorted(registry["packs"]):
        record = registry["packs"][pack_id]
        if not record["enabled"]:
            continue
        version = record["selected"]
        try:
            root, manifest = store.verify(registry, pack_id, version)
            grants = record["grants"]
            check_activation(manifest, grants["origins"], grants["capabilities"],
                             grants["dependencies"])
            config = resolve_config(manifest, record["config"])
            entry = manifest["entrypoints"].get("command")
            if entry is None:
                continue
            for declaration in entry["commands"]:
                commands.append(Command(
                    path=tuple(declaration["path"]), summary=declaration["summary"],
                    usage=declaration["usage"], profile=declaration["profile"],
                    provider_id=pack_id, provider_version=version, source="pack",
                    root=root, file=entry["file"], runtime=entry["runtime"], config=config,
                    grants=json.loads(json.dumps(grants)),
                ))
        except (PackError, OSError, ValueError) as error:
            diagnostics.append(f"{pack_id}@{version}: {error}")
    return commands, diagnostics


def discover(profile_root=None):
    commands = builtin_commands()
    diagnostics = []
    if profile_root is None:
        return Registry(commands, diagnostics)
    external, diagnostics = _pack_commands(Path(profile_root).resolve())
    accepted = []
    for command in external:
        conflict = next((other for other in [*accepted, *commands.values()]
                         if (command.path[0] in CORE_ROOTS
                             or _paths_conflict(command.path, other.path))), None)
        if conflict is not None:
            diagnostics.append(
                f"{command.provider_id}@{command.provider_version}: command {command.label} "
                f"conflicts with {conflict.label} from {conflict.provider_id}")
            continue
        commands[command.path] = command
        accepted.append(command)
    return Registry(commands, diagnostics)


def render_help(registry, prefix=(), stream=None):
    stream = stream or sys.stdout
    prefix = tuple(prefix)
    exact = registry.commands.get(prefix)
    if exact is not None:
        invocation = "tap --profile PROFILE " + exact.label
        if exact.usage:
            invocation += " " + exact.usage
        print(f"usage: {invocation}", file=stream)
        print(file=stream)
        print(exact.summary, file=stream)
        print(f"provider: {exact.provider_id}@{exact.provider_version} ({exact.source})", file=stream)
        print(f"profile: {exact.profile}; command API: {COMMAND_API}", file=stream)
        print("arguments after the command path are passed unchanged; help does not run provider code",
              file=stream)
        return
    matches = registry.children(prefix)
    heading = "available commands" if not prefix else f"commands under {' '.join(prefix)}"
    print(heading + ":", file=stream)
    if not matches:
        print("  (none)", file=stream)
    for command in matches:
        print(f"  {command.label:<28} {command.summary} "
              f"[{command.provider_id}@{command.provider_version}]", file=stream)
    for diagnostic in registry.diagnostics:
        print(f"warning: {diagnostic}", file=sys.stderr)


def _private_runtime_directory(store, path):
    path = store._safe_profile_path(path)
    if path.is_symlink():
        raise TapError(f"Command runtime path must not be a symlink: {path}")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def _run_pack(command, profile_root, argv, lease_fd=None):
    from .pack_store import PackStore
    profile_root = Path(profile_root).expanduser().resolve()
    store = PackStore(profile_root)
    state = _private_runtime_directory(store, profile_root / "state/packs" / command.provider_id)
    output = _private_runtime_directory(store, profile_root / "data/packs" / command.provider_id)
    logs = _private_runtime_directory(store, profile_root / "logs/packs" / command.provider_id)
    executable = (command.root / command.file).resolve()
    if (not executable.is_file() or executable.is_symlink()
            or executable.parent != command.root.resolve()):
        raise TapError(f"Command entrypoint is missing or unsafe: {command.provider_id}")
    context = {
        "command_api": COMMAND_API,
        "path": list(command.path),
        "provider": {"id": command.provider_id, "version": command.provider_version},
        "profile": str(profile_root),
        "state_dir": str(state), "output_dir": str(output), "log_dir": str(logs),
        "config": command.config, "grants": command.grants,
        "runtime": command.runtime,
    }
    environment = dict(os.environ)
    environment.update({
        "TAP_COMMAND_CONTEXT": json.dumps(context, separators=(",", ":")),
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    process = None
    try:
        if command.runtime != "host-python":
            raise TapError(f"Unsupported command runtime: {command.runtime}")
        process = subprocess.Popen(
            [sys.executable, "-B", str(executable), *argv], cwd=state,
            env=environment, stdin=None, stdout=None, stderr=None,
            pass_fds=(() if lease_fd is None else (lease_fd,)),
        )
        returncode = process.wait()
    except KeyboardInterrupt:
        if process is not None and process.poll() is None:
            process.send_signal(signal.SIGINT)
        if process is not None:
            process.wait()
        return 130
    except OSError as error:
        raise TapError(f"Cannot start command provider {command.provider_id}: {error}") from error
    return 128 + (-returncode) if returncode < 0 else returncode


def execute(command, profile_root, argv, lease_fd=None):
    if command.source == "builtin":
        if command.path == ("where",):
            return _where(profile_root, argv)
        raise TapError(f"Unknown built-in command provider: {command.label}")
    return _run_pack(command, profile_root, argv, lease_fd=lease_fd)


def dispatch(profile_root, words):
    """Resolve once for help, then again under the profile lock for execution."""
    profile_root = Path(profile_root).expanduser().resolve()
    registry = discover(profile_root)
    command = registry.match(words)
    if command is None:
        if words and words[-1] in ("-h", "--help"):
            render_help(registry, words[:-1])
            return 0
        return None
    argv = list(words[len(command.path):])
    if argv and argv[0] in ("-h", "--help"):
        render_help(registry, command.path)
        return 0
    if command.source == "builtin":
        return execute(command, profile_root, argv)
    with profile_lock(
            profile_root,
            busy_message="A command from this profile is still running; pack changes wait for it to finish") as lease:
        current = discover(profile_root).commands.get(command.path)
        if current is None or (current.provider_id, current.provider_version) != (
                command.provider_id, command.provider_version):
            raise TapError("Command provider changed before invocation; retry the command")
        return execute(current, profile_root, argv, lease_fd=lease.fileno())
