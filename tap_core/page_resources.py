"""Versioned provider contract for immutable browser resources."""

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re


CONTRACT = "tap.page-resource/v1"
RESOURCE_ID = re.compile(r"[a-z][a-z0-9]*(?:[.-][a-z0-9]+)*\Z")
VERSION = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
KINDS = {"browser-classic-script"}


class ResourceError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise ResourceError(message)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, f"resource JSON: duplicate key {key!r}")
        result[key] = value
    return result


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def resource_path(root, name):
    require(type(name) is str and bool(name), "resource file must be a relative path")
    relative = PurePosixPath(name)
    require(not relative.is_absolute() and ".." not in relative.parts
            and str(relative) == name and "\\" not in name and "\x00" not in name,
            f"resource file is unsafe or noncanonical: {name!r}")
    root = Path(root).resolve()
    path = (root / name).resolve()
    require(root in path.parents and path.is_file(), f"resource file is missing or outside root: {name}")
    return path


def validate_resource(value, root, declared_files=None):
    required = {"contract", "id", "version", "kind", "file", "sha256", "license",
                "source_revision"}
    require(type(value) is dict and set(value) == required,
            f"resource declaration requires exactly {sorted(required)}")
    require(value["contract"] == CONTRACT, f"resource contract must be {CONTRACT}")
    require(type(value["id"]) is str and RESOURCE_ID.fullmatch(value["id"]),
            "resource id is invalid")
    require(type(value["version"]) is str and VERSION.fullmatch(value["version"]),
            "resource version must be exact MAJOR.MINOR.PATCH")
    require(type(value["kind"]) is str and value["kind"] in KINDS,
            "resource kind is unsupported")
    require(type(value["sha256"]) is str and DIGEST.fullmatch(value["sha256"]),
            "resource sha256 must be 64 lowercase hexadecimal characters")
    for name in ("license", "source_revision"):
        require(type(value[name]) is str and bool(value[name].strip()),
                f"resource {name} must be a nonempty string")
    if declared_files is not None:
        require(value["file"] in declared_files, "resource file must be declared in pack files")
    path = resource_path(root, value["file"])
    require(digest(path) == value["sha256"],
            f"resource bytes do not match sha256: {value['id']}@{value['version']}")
    return value


def load_resource(root):
    root = Path(root).resolve()
    try:
        value = json.loads((root / "tap-resource.json").read_text(encoding="utf-8"),
                           object_pairs_hook=unique_object)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ResourceError(f"tap-resource.json: {error}") from error
    return validate_resource(value, root)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("provider", type=Path)
    args = parser.parse_args(argv)
    try:
        value = load_resource(args.provider)
    except ResourceError as error:
        parser.exit(1, f"tap page resource: {error}\n")
    print(json.dumps({"valid": True, "contract": value["contract"], "id": value["id"],
                      "version": value["version"], "sha256": value["sha256"]}, indent=2))


if __name__ == "__main__":
    main()
