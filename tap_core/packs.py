"""Experimental pack manifest v1 validation; does not install or execute packs."""

import argparse
import json
from pathlib import Path, PurePosixPath
import re
from urllib.parse import urlsplit


PACK_API = 1
ID = re.compile(r"[a-z][a-z0-9]*(?:[.-][a-z0-9]+)*\Z")
VERSION = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z")
ROLES = {
    "reader": (("python-jsonl-v1",), "capture.read"),
    "mutator": (("mitmproxy-python",), "response.mutate"),
    # browser-module-v1 remains the executable fixture interface. The classic
    # interface matches the proven profile bridge: an ordered, startup-time
    # snapshot of trusted scripts rather than a pretend module lifecycle.
    "page": (("browser-module-v1", "browser-scripts-v1"), "page.inject"),
    "handler": (("python-jsonl-v1",), "bridge.handle"),
}
TYPES = {"string": str, "integer": int, "boolean": bool}


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
                      "entrypoints", "config", "access"))
    require(type(manifest["manifest_version"]) is int and manifest["manifest_version"] == 1,
            "manifest_version: supported version is 1")
    require(type(manifest["id"]) is str and ID.fullmatch(manifest["id"]), "id: invalid pack id")
    require(type(manifest["version"]) is str and VERSION.fullmatch(manifest["version"]),
            "version: expected release version MAJOR.MINOR.PATCH")
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
    entries = manifest["entrypoints"]
    require(type(entries) is dict and bool(entries), "entrypoints: expected nonempty object")
    require(not (entries.keys() - ROLES.keys()), "entrypoints: unsupported role")
    for role, entry in entries.items():
        require(type(entry) is dict and type(entry.get("interface")) is str,
                f"entrypoints.{role}: expected object with interface")
        interface = entry["interface"]
        require(interface in ROLES[role][0], f"entrypoints.{role}: unsupported interface")
        if role == "page" and interface == "browser-scripts-v1":
            fields(entry, ("interface", "scripts"), label=f"entrypoints.{role}")
            require(type(entry["scripts"]) is list and bool(entry["scripts"]),
                    f"entrypoints.{role}.scripts: expected nonempty array")
            script_ids = set()
            for script in entry["scripts"]:
                fields(script, ("id", "version", "file"),
                       label=f"entrypoints.{role}.scripts[]")
                require(type(script["id"]) is str and ID.fullmatch(script["id"]),
                        f"entrypoints.{role}.scripts[].id: invalid resource id")
                require(script["id"] not in script_ids,
                        f"entrypoints.{role}.scripts: duplicate resource id {script['id']}")
                script_ids.add(script["id"])
                require(type(script["version"]) is str and VERSION.fullmatch(script["version"]),
                        f"entrypoints.{role}.scripts[].version: expected MAJOR.MINOR.PATCH")
                require(type(script["file"]) is str and script["file"] in manifest["files"],
                        f"entrypoints.{role}.scripts[].file: must be declared in files")
        else:
            fields(entry, ("file", "interface"), label=f"entrypoints.{role}")
            require(type(entry["file"]) is str and entry["file"] in manifest["files"],
                    f"entrypoints.{role}: file must be declared in files")
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
        exact_origin(origin)
    strings(access["capabilities"], "access.capabilities")
    supported = {value[1] for value in ROLES.values()}
    require(set(access["capabilities"]) <= supported, "access.capabilities: unknown capability")
    for role in entries:
        require(ROLES[role][1] in access["capabilities"], f"access: missing capability for {role}")
    require(bool(access["origins"]), "access.origins: at least one exact origin required")
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
    for category, granted in (("origins", granted_origins), ("capabilities", granted_capabilities)):
        missing = set(manifest["access"][category]) - set(granted)
        require(not missing, f"access.{category}: not granted: {sorted(missing)}")
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
