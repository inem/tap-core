"""Experimental pack manifest v1 validation; does not install or execute packs."""

import argparse
import json
from pathlib import Path, PurePosixPath
import re
from urllib.parse import urlsplit

from .page_resources import ResourceError, validate_resource


PACK_API = 1
ALL_ORIGINS = "*"
ID = re.compile(r"[a-z][a-z0-9]*(?:[.-][a-z0-9]+)*\Z")
VERSION = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z")
COMMAND_SEGMENT = re.compile(r"[a-z][a-z0-9-]*\Z")
ROLES = {
    "reader": (("python-jsonl-v1",), "capture.read"),
    "mutator": (("mitmproxy-python",), "response.mutate"),
    # browser-module-v1 remains the executable fixture interface. The classic
    # interface matches the proven profile bridge: an ordered, startup-time
    # snapshot of trusted scripts rather than a pretend module lifecycle.
    "page": (("browser-module-v1", "browser-scripts-v1"), "page.inject"),
    "handler": (("python-jsonl-v1",), "bridge.handle"),
    "command": (("process-argv-v1",), "command.execute"),
}
TYPES = {"string": str, "integer": int, "boolean": bool}
TOOL_ANNOTATIONS = frozenset({
    "readOnlyHint", "untrustedContentHint", "consequentialHint", "debugging",
})


class PackError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise PackError(message)


def fields(value, required, optional=(), label="manifest"):
    require(type(value) is dict, f"{label}: expected object")
    require(all(type(key) is str for key in value), f"{label}: keys must be strings")
    missing = set(required) - value.keys()
    extra = value.keys() - set(required) - set(optional)
    require(not missing, f"{label}: missing fields {sorted(missing)}")
    require(not extra, f"{label}: unknown fields {sorted(extra)}")


def strings(value, label):
    require(type(value) is list and all(type(item) is str for item in value),
            f"{label}: expected array of strings")
    require(len(value) == len(set(value)), f"{label}: duplicate values")


def validate_features(value):
    require(type(value) is list and len(value) <= 32,
            "features: expected at most 32 feature declarations")
    identifiers = set()
    for feature in value:
        fields(feature, ("id", "label", "value"), ("folder",), label="features[]")
        require(type(feature["id"]) is str and ID.fullmatch(feature["id"]),
                "features[].id: invalid identifier")
        require(feature["id"] not in identifiers, "features[].id: duplicate")
        identifiers.add(feature["id"])
        require(type(feature["label"]) is str and 1 <= len(feature["label"]) <= 80,
                "features[].label: expected 1..80 characters")
        require(type(feature["value"]) is str and 1 <= len(feature["value"]) <= 160,
                "features[].value: expected 1..160 characters")
        if "folder" in feature:
            folder = feature["folder"]
            require(type(folder) is str and 1 <= len(folder) <= 240,
                    "features[].folder: expected 1..240 characters")
            path = PurePosixPath(folder)
            require(not path.is_absolute() and ".." not in path.parts and str(path) == folder
                    and "\\" not in folder and "\x00" not in folder
                    and path.parts and path.parts[0] == "data",
                    "features[].folder: expected a canonical path under profile data")


def validate_tools(value):
    """Validate declarative WebMCP-shaped TAP tool descriptors.

    These describe a pack's existing bridge binding. They do not grant access,
    create an executable function, or register a native WebMCP tool.
    """
    require(type(value) is list and len(value) <= 32,
            "tools: expected at most 32 tool declarations")
    names = set()
    for tool in value:
        fields(tool, ("name", "description", "inputSchema"),
               ("title", "annotations"), label="tools[]")
        name = tool["name"]
        require(type(name) is str and 1 <= len(name) <= 128 and name == name.strip()
                and "\x00" not in name,
                "tools[].name: expected a nonempty canonical tool name")
        require(name not in names, "tools[].name: duplicate")
        names.add(name)
        if "title" in tool:
            require(type(tool["title"]) is str and 1 <= len(tool["title"]) <= 256,
                    "tools[].title: expected 1..256 characters")
        require(type(tool["description"]) is str and 1 <= len(tool["description"]) <= 1024,
                "tools[].description: expected 1..1024 characters")
        schema = tool["inputSchema"]
        require(type(schema) is dict and schema.get("type") == "object",
                "tools[].inputSchema: expected an object JSON Schema")
        try:
            encoded = json.dumps(schema, separators=(",", ":"), ensure_ascii=False)
        except (TypeError, ValueError) as error:
            raise PackError("tools[].inputSchema: must be JSON-serializable") from error
        require(len(encoded.encode("utf-8")) <= 8192,
                "tools[].inputSchema: exceeds 8 KiB")
        annotations = tool.get("annotations", {})
        require(type(annotations) is dict and set(annotations) <= TOOL_ANNOTATIONS
                and all(type(value) is bool for value in annotations.values()),
                "tools[].annotations: expected WebMCP boolean hints")


def exact_origin(value):
    """Canonical http(s) origin only; no wildcard, path, credentials or query."""
    try:
        origin = urlsplit(value)
        host = origin.hostname or ""
        port = origin.port
        require(origin.scheme in ("http", "https") and bool(host), "invalid origin")
        require(not origin.username and not origin.password, "origin has credentials")
        require(not origin.path and not origin.query and not origin.fragment,
                "origin must not have a path, query or fragment")
        require(re.fullmatch(r"[a-z0-9]+(?:[.-][a-z0-9]+)*", host) is not None,
                "origin host must be an exact lowercase ASCII hostname (no wildcard)")
        require(port is None or port > 0, "origin port must be positive")
        require(port != {"http": 80, "https": 443}[origin.scheme],
                "omit the default origin port")
        expected = f"{origin.scheme}://{host}" + (f":{port}" if port else "")
        require(value == expected, "origin must be canonical")
    except (ValueError, TypeError) as error:
        raise PackError(f"access.origins: {value!r}: {error}") from error


def exact_or_all_origin(value):
    if value == ALL_ORIGINS:
        return value
    return exact_origin(value)


def origin_granted(requested, granted):
    """Host grants are explicit; '*' means every HTTP(S) origin."""
    return requested == ALL_ORIGINS and ALL_ORIGINS in granted or requested in granted or ALL_ORIGINS in granted


TRIGGER_DEFAULTS = {"debounce_ms": 500, "min_interval_seconds": 10}
TRIGGER_BACKSTOP_SECONDS = 3600


def validate_trigger(trigger):
    """A command trigger: host paths (watch) and/or captured records (capture)
    whose arrival requests a run. Capture origins are checked against the
    pack's grants in validate_capture_trigger, once access is validated."""
    fields(trigger, (), optional=("watch", "capture", *TRIGGER_DEFAULTS), label="command.trigger")
    require("watch" in trigger or "capture" in trigger, "command.trigger: expected watch and/or capture")
    if "watch" in trigger:
        strings(trigger["watch"], "command.trigger.watch")
        require(1 <= len(trigger["watch"]) <= 16, "command.trigger.watch: expected 1..16 paths")
        for path in trigger["watch"]:
            parts = PurePosixPath(path).parts
            require(0 < len(path) <= 1024 and "\x00" not in path and ".." not in parts
                    and all(character.isprintable() for character in path)
                    and (path.startswith(("/", "~/")) or str(PurePosixPath(path)) == path),
                    "command.trigger.watch: expected '~/…', an absolute path, or a canonical "
                    "profile-relative path without '..'")
    if "capture" in trigger:
        capture = trigger["capture"]
        fields(capture, ("origins",), optional=("path_prefixes",), label="command.trigger.capture")
        strings(capture["origins"], "command.trigger.capture.origins")
        require(1 <= len(capture["origins"]) <= 64, "command.trigger.capture.origins: expected 1..64 origins")
        for origin in capture["origins"]:
            exact_origin(origin)
        prefixes = capture.get("path_prefixes", {})
        require(type(prefixes) is dict, "command.trigger.capture.path_prefixes: expected object keyed by origin")
        for origin, paths in prefixes.items():
            require(origin in capture["origins"],
                    f"command.trigger.capture.path_prefixes: {origin} is not in capture.origins")
            validate_path_prefixes(paths, f"command.trigger.capture.path_prefixes[{origin}]")
    require("debounce_ms" not in trigger
            or type(trigger["debounce_ms"]) is int and 0 <= trigger["debounce_ms"] <= 60000,
            "command.trigger.debounce_ms must be 0..60000")
    require("min_interval_seconds" not in trigger
            or type(trigger["min_interval_seconds"]) is int and 1 <= trigger["min_interval_seconds"] <= 86400,
            "command.trigger.min_interval_seconds must be 1..86400")


def validate_capture_trigger(trigger, access):
    for origin in trigger.get("capture", {}).get("origins", []):
        require(origin_granted(origin, set(access["origins"])),
                f"command.trigger.capture.origins: {origin} is not in access.origins")


def validate_path_prefixes(paths, label):
    strings(paths, label)
    require(1 <= len(paths) <= 64, f"{label}: expected 1..64 prefixes")
    for path in paths:
        require(path.startswith("/") and len(path) <= 512 and "?" not in path and "#" not in path
                and all(character.isprintable() for character in path),
                f"{label}: expected printable URL path prefixes")


def command_schedule(declaration):
    """The effective schedule of a command declaration, defaults applied.

    A declaration without a trigger yields its schedule unchanged, so existing
    scheduled commands keep their fingerprint and timing.
    """
    schedule = declaration.get("schedule")
    if schedule is None or "trigger" not in declaration:
        return schedule
    return {"interval_seconds": schedule.get("interval_seconds", TRIGGER_BACKSTOP_SECONDS),
            "timeout_seconds": schedule["timeout_seconds"],
            "trigger": {**({"watch": list(declaration["trigger"]["watch"])}
                           if "watch" in declaration["trigger"] else {}),
                        **({"capture": {"origins": list(declaration["trigger"]["capture"]["origins"]),
                                        "path_prefixes": {origin: list(paths) for origin, paths in
                                                          declaration["trigger"]["capture"]
                                                          .get("path_prefixes", {}).items()}}}
                           if "capture" in declaration["trigger"] else {}),
                        **{name: declaration["trigger"].get(name, default)
                           for name, default in TRIGGER_DEFAULTS.items()}}}


def validate_reader_input(value, access):
    """Optional per-origin URL path prefixes a reader is limited to."""
    fields(value, ("path_prefixes",), label="entrypoints.reader.input")
    prefixes = value["path_prefixes"]
    require(type(prefixes) is dict and bool(prefixes),
            "entrypoints.reader.input.path_prefixes: expected nonempty object keyed by origin")
    for origin, paths in prefixes.items():
        exact_origin(origin)
        require(origin_granted(origin, set(access["origins"])),
                f"entrypoints.reader.input.path_prefixes: {origin} is not in access.origins")
        validate_path_prefixes(paths, f"entrypoints.reader.input.path_prefixes[{origin}]")


def pack_file(root, name):
    require(type(name) is str and bool(name), "files: expected nonempty relative path")
    path = PurePosixPath(name)
    require(not path.is_absolute() and ".." not in path.parts and str(path) == name
            and "\\" not in name and "\x00" not in name,
            f"files: unsafe or noncanonical path {name!r}")
    resolved = (root / name).resolve()
    require(root in resolved.parents and resolved.is_file(),
            f"files: missing file or path outside pack: {name}")
    return resolved


def validate_manifest(manifest, root, host_api=PACK_API):
    """Validate declarations and packaged files without importing any pack code."""
    root = Path(root).resolve()
    fields(manifest, ("manifest_version", "id", "version", "requires", "files",
                      "entrypoints", "config", "access"),
           optional=("resources", "features", "tools", "capture"))
    require(type(manifest["manifest_version"]) is int and manifest["manifest_version"] == 1,
            "manifest_version: supported version is 1")
    require(type(manifest["id"]) is str and ID.fullmatch(manifest["id"]), "id: invalid pack id")
    require(type(manifest["version"]) is str and VERSION.fullmatch(manifest["version"]),
            "version: expected release version MAJOR.MINOR.PATCH")
    validate_features(manifest.get("features", []))
    validate_tools(manifest.get("tools", []))
    requires = manifest["requires"]
    fields(requires, ("pack_api", "dependencies"), label="requires")
    require(type(requires["pack_api"]) is int and requires["pack_api"] == host_api,
            f"requires.pack_api: incompatible with host API {host_api}")
    require(type(requires["dependencies"]) is list, "requires.dependencies: expected array")
    names = set()
    for dependency in requires["dependencies"]:
        fields(dependency, ("id", "version"), label="dependency")
        require(type(dependency["id"]) is str and ID.fullmatch(dependency["id"]),
                "dependency.id: invalid identifier")
        require(dependency["id"] != manifest["id"], "dependency.id: pack cannot depend on itself")
        require(dependency["id"] not in names, "dependency.id: duplicate")
        names.add(dependency["id"])
        require(type(dependency["version"]) is str and VERSION.fullmatch(dependency["version"]),
                "dependency.version: exact MAJOR.MINOR.PATCH required")
    strings(manifest["files"], "files")
    require(bool(manifest["files"]), "files: at least one file required")
    require(len(manifest["files"]) <= 256, "files: at most 256 files supported")
    for name in manifest["files"]:
        pack_file(root, name)
    resources = manifest.get("resources", [])
    require(type(resources) is list, "resources: expected array")
    resource_index = {}
    for resource in resources:
        try:
            validate_resource(resource, root, set(manifest["files"]))
        except ResourceError as error:
            raise PackError(f"resources: {error}") from error
        key = (resource["id"], resource["version"])
        require(key not in resource_index,
                f"resources: duplicate provider {resource['id']}@{resource['version']}")
        resource_index[key] = resource
    entries = manifest["entrypoints"]
    require(type(entries) is dict and bool(entries), "entrypoints: expected nonempty object")
    require(not (entries.keys() - ROLES.keys()), "entrypoints: unsupported role")
    for role, entry in entries.items():
        require(type(entry) is dict and type(entry.get("interface")) is str,
                f"entrypoints.{role}: expected object with interface")
        interface = entry["interface"]
        require(interface in ROLES[role][0], f"entrypoints.{role}: unsupported interface")
        if role == "page" and interface == "browser-scripts-v1":
            fields(entry, ("interface", "uses"), label=f"entrypoints.{role}")
            require(type(entry["uses"]) is list and bool(entry["uses"]),
                    f"entrypoints.{role}.uses: expected nonempty array")
            use_ids = set()
            for use in entry["uses"]:
                fields(use, ("id", "version"), label=f"entrypoints.{role}.uses[]")
                require(type(use["id"]) is str and ID.fullmatch(use["id"]),
                        f"entrypoints.{role}.uses[].id: invalid resource id")
                require(use["id"] not in use_ids,
                        f"entrypoints.{role}.uses: duplicate resource id {use['id']}")
                use_ids.add(use["id"])
                require(type(use["version"]) is str and VERSION.fullmatch(use["version"]),
                        f"entrypoints.{role}.uses[].version: expected MAJOR.MINOR.PATCH")
                require((use["id"], use["version"]) in resource_index,
                        f"entrypoints.{role}.uses[]: missing provider "
                        f"{use['id']}@{use['version']}")
        elif role == "command":
            fields(entry, ("file", "interface", "runtime", "commands"), optional=("session_headers",), label=f"entrypoints.{role}")
            require(type(entry["file"]) is str and entry["file"] in manifest["files"],
                    f"entrypoints.{role}: file must be declared in files")
            require(entry["runtime"] == "host-python",
                    "entrypoints.command.runtime: supported binding is host-python")
            require(type(entry["commands"]) is list and bool(entry["commands"]),
                    "entrypoints.command.commands: expected nonempty array")
            paths = set()
            for declaration in entry["commands"]:
                fields(declaration, ("path", "summary", "usage", "profile"),
                       optional=("schedule", "trigger"), label="entrypoints.command.commands[]")
                if "trigger" in declaration:
                    require("schedule" in declaration, "command.trigger requires command.schedule")
                    validate_trigger(declaration["trigger"])
                if "schedule" in declaration:
                    schedule = declaration["schedule"]
                    # With a trigger the interval is an optional backstop.
                    fields(schedule, ("timeout_seconds",) if "trigger" in declaration
                           else ("interval_seconds", "timeout_seconds"),
                           optional=("interval_seconds",), label="command.schedule")
                    require("interval_seconds" not in schedule
                            or type(schedule["interval_seconds"]) is int and 10 <= schedule["interval_seconds"] <= 86400,
                            "command.schedule interval must be 10..86400 seconds")
                    require(type(schedule["timeout_seconds"]) is int and 1 <= schedule["timeout_seconds"] <= 3600,
                            "command.schedule timeout must be 1..3600 seconds")
                path = declaration["path"]
                require(type(path) is list and bool(path)
                        and all(type(segment) is str and COMMAND_SEGMENT.fullmatch(segment)
                                for segment in path),
                        "entrypoints.command.commands[].path: expected nonempty lowercase command words")
                command_path = tuple(path)
                require(command_path not in paths,
                        f"entrypoints.command.commands: duplicate path {' '.join(path)}")
                paths.add(command_path)
                for name, limit in (("summary", 160), ("usage", 240)):
                    value = declaration[name]
                    require(type(value) is str and bool(value.strip()) and len(value) <= limit
                            and all(character.isprintable() for character in value),
                            f"entrypoints.command.commands[].{name}: expected printable text")
                require(declaration["profile"] == "required",
                        "entrypoints.command.commands[].profile: v1 requires 'required'")
        else:
            fields(entry, ("file", "interface"), optional=("delivery", "input") if role == "reader" else (),
                   label=f"entrypoints.{role}")
            require(type(entry["file"]) is str and entry["file"] in manifest["files"],
                    f"entrypoints.{role}: file must be declared in files")
            if role == "reader" and "delivery" in entry:
                require(entry["delivery"] == "fresh-and-replay-v1",
                        "entrypoints.reader.delivery: unsupported mode")
    config = manifest["config"]
    require(type(config) is dict, "config: expected object")
    for name, setting in config.items():
        require(type(name) is str and ID.fullmatch(name), "config: invalid key")
        fields(setting, ("type", "default"), label=f"config.{name}")
        require(type(setting["type"]) is str and setting["type"] in TYPES,
                f"config.{name}: unsupported type")
        require(type(setting["default"]) is TYPES[setting["type"]],
                f"config.{name}: default has wrong type")
    access = manifest["access"]
    fields(access, ("origins", "capabilities"), label="access")
    strings(access["origins"], "access.origins")
    for origin in access["origins"]:
        exact_or_all_origin(origin)
    strings(access["capabilities"], "access.capabilities")
    if "input" in entries.get("reader", {}):
        validate_reader_input(entries["reader"]["input"], access)
    for declaration in entries.get("command", {}).get("commands", []):
        if "trigger" in declaration:
            validate_capture_trigger(declaration["trigger"], access)
    capture = manifest.get("capture")
    if capture is not None:
        fields(capture, ("request_body_paths",), label="capture")
        strings(capture["request_body_paths"], "capture.request_body_paths")
        require(bool(capture["request_body_paths"]),
                "capture.request_body_paths: at least one path required")
        require(len(capture["request_body_paths"]) <= 64,
                "capture.request_body_paths: at most 64 paths")
        for path in capture["request_body_paths"]:
            require(path.startswith("/") and len(path) <= 512 and "#" not in path
                    and all(character.isprintable() for character in path),
                    "capture.request_body_paths: expected printable URL path substrings")
        require("capture.read" in access["capabilities"],
                "capture.request_body_paths requires capture.read")
        require(ALL_ORIGINS not in access["origins"],
                "capture.request_body_paths requires exact origins, not '*'")
    if any("schedule" in declaration for declaration in entries.get("command", {}).get("commands", [])):
        require("background.run" in access["capabilities"], "scheduled commands require background.run")
    observation = entries.get("command", {}).get("session_headers")
    if observation is not None:
        fields(observation, ("path_prefix", "headers"), label="command.session_headers")
        require(type(observation["path_prefix"]) is str and observation["path_prefix"].startswith("/")
                and observation["path_prefix"].endswith("/") and "?" not in observation["path_prefix"],
                "session_headers: use an absolute directory path prefix")
        strings(observation["headers"], "session_headers.headers")
        require(bool(observation["headers"]) and set(observation["headers"]) <= {"cookie", "authorization", "user-agent"},
                "session_headers: unsupported header")
        require("session.observe" in access["capabilities"], "session_headers requires session.observe")
        require(len(access["origins"]) == 1 and access["origins"][0].startswith("https://")
                and ":" not in access["origins"][0][8:],
                "session_headers requires one HTTPS origin with default port")
    supported = {value[1] for value in ROLES.values()} | {"background.run", "session.observe"}
    require(set(access["capabilities"]) <= supported, "access.capabilities: unknown capability")
    for role in entries:
        require(ROLES[role][1] in access["capabilities"], f"access: missing capability for {role}")
    require(bool(access["origins"]), "access.origins: at least one origin required")
    return manifest


def no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, f"JSON: duplicate key {key!r}")
        result[key] = value
    return result


def load_manifest(root, host_api=PACK_API):
    root = Path(root).resolve()
    try:
        manifest = json.loads((root / "pack.json").read_text(encoding="utf-8"),
                              object_pairs_hook=no_duplicate_keys)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise PackError(f"pack.json: {error}") from error
    return validate_manifest(manifest, root, host_api)


def resolve_config(manifest, overrides=None):
    overrides = {} if overrides is None else overrides
    require(type(overrides) is dict, "config overrides: expected object")
    require(not (overrides.keys() - manifest["config"].keys()), "config overrides: unknown key")
    result = {}
    for name, setting in manifest["config"].items():
        value = overrides.get(name, setting["default"])
        require(type(value) is TYPES[setting["type"]], f"config.{name}: override has wrong type")
        result[name] = value
    return result


def check_activation(manifest, granted_origins, granted_capabilities, dependencies):
    """Check a supplied host policy/inventory; never infer grants from requests."""
    missing_origins = [origin for origin in manifest["access"]["origins"]
                       if not origin_granted(origin, set(granted_origins))]
    require(not missing_origins, f"access.origins: not granted: {sorted(missing_origins)}")
    missing_capabilities = set(manifest["access"]["capabilities"]) - set(granted_capabilities)
    require(not missing_capabilities, f"access.capabilities: not granted: {sorted(missing_capabilities)}")
    for dependency in manifest["requires"]["dependencies"]:
        require(dependency["id"] != manifest["id"], "dependency.id: pack cannot depend on itself")
        require(dependencies.get(dependency["id"]) == dependency["version"],
                f"dependency {dependency['id']}: expected installed version {dependency['version']}")


def fixture_context(manifest, profile, overrides=None):
    """Return explicit host-owned paths; creates no directories and runs no code."""
    profile = Path(profile).resolve()
    return {"pack_api": PACK_API, "pack_id": manifest["id"],
            "state_dir": str(profile / "state" / "packs" / manifest["id"]),
            "output_dir": str(profile / "data" / "packs" / manifest["id"]),
            "log_dir": str(profile / "logs" / "packs" / manifest["id"]),
            "config": resolve_config(manifest, overrides)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pack", type=Path)
    parser.add_argument("--host-api", type=int, default=PACK_API)
    args = parser.parse_args()
    try:
        manifest = load_manifest(args.pack, args.host_api)
    except PackError as error:
        parser.exit(1, f"invalid pack: {error}\n")
    print(json.dumps({"id": manifest["id"], "version": manifest["version"],
                      "manifest_valid": True, "activated": False}))


if __name__ == "__main__":
    main()
