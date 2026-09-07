"""Immutable pack artifacts and profile-local activation state.

The first installed binding is deliberately narrow: ordered classic browser
scripts on the existing profile bridge. Other manifest roles remain valid but
cannot be enabled until their host bindings are implemented.
"""

import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import tarfile
import tempfile

from .packs import (ID, VERSION, PackError, check_activation, load_manifest,
                    no_duplicate_keys, require, resolve_config)


REGISTRY_VERSION = 1
MAX_ARCHIVE_FILES = 257  # pack.json plus the manifest's 256 files
MAX_ARCHIVE_BYTES = 32 * 1024 * 1024


def _private_directory(path):
    path = Path(path)
    if path.is_symlink():
        raise PackError(f"Pack directory must not be a symlink: {path}")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def _canonical_name(name):
    path = PurePosixPath(name)
    require(type(name) is str and bool(name) and not path.is_absolute()
            and ".." not in path.parts and str(path) == name
            and "\\" not in name and "\x00" not in name,
            f"artifact: unsafe or noncanonical path {name!r}")
    return path


def _digest(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _declared_names(manifest):
    return ["pack.json", *manifest["files"]]


def _tree_hashes(root, manifest):
    root = Path(root).resolve()
    expected = set(_declared_names(manifest))
    observed = set()
    for path in root.rglob("*"):
        if path.is_symlink():
            raise PackError(f"installed pack contains a symlink: {path.relative_to(root)}")
        if path.is_file():
            observed.add(path.relative_to(root).as_posix())
    require(observed == expected,
            f"installed pack file set changed: expected {sorted(expected)}, got {sorted(observed)}")
    return {name: _digest(root / name) for name in sorted(expected)}


def build_artifact(source, output):
    """Build a deterministic gzip-compressed tar artifact from declared files."""
    source, output = Path(source).resolve(), Path(output).resolve()
    manifest = load_manifest(source)
    require(not output.exists(), f"artifact output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=".tap-pack-", dir=output.parent)
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        with temporary.open("wb") as raw:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
                with tarfile.open(mode="w", fileobj=compressed, format=tarfile.PAX_FORMAT) as archive:
                    for name in sorted(_declared_names(manifest)):
                        data = (source / name).read_bytes()
                        info = tarfile.TarInfo(name)
                        info.size = len(data)
                        info.mode = 0o644
                        info.mtime = info.uid = info.gid = 0
                        info.uname = info.gname = ""
                        archive.addfile(info, io.BytesIO(data))
        temporary.chmod(0o600)
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    return {"artifact": str(output), "id": manifest["id"], "version": manifest["version"],
            "sha256": _digest(output), "files": len(_declared_names(manifest))}


def _extract_artifact(artifact, destination):
    names, total = set(), 0
    try:
        with tarfile.open(Path(artifact), mode="r:*") as archive:
            members = archive.getmembers()
            require(len(members) <= MAX_ARCHIVE_FILES, "artifact: too many entries")
            for member in members:
                name = member.name
                _canonical_name(name)
                require(member.isfile(), f"artifact: only regular files are allowed: {name}")
                require(name not in names, f"artifact: duplicate path {name}")
                names.add(name)
                total += member.size
                require(total <= MAX_ARCHIVE_BYTES, "artifact: uncompressed content exceeds 32 MiB")
                target = destination / name
                target.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                require(source is not None, f"artifact: cannot read {name}")
                with target.open("xb") as handle:
                    shutil.copyfileobj(source, handle)
    except (OSError, tarfile.TarError) as error:
        raise PackError(f"artifact: {error}") from error
    manifest = load_manifest(destination)
    require(names == set(_declared_names(manifest)),
            "artifact: entries must be exactly pack.json plus manifest files")
    return manifest


class PackStore:
    def __init__(self, profile_root):
        self.root = Path(profile_root).resolve()
        self.code = self.root / "packs"
        self.registry_file = self.root / "state/pack-registry.json"

    def _empty(self):
        return {"version": REGISTRY_VERSION, "packs": {}}

    def load(self):
        try:
            value = json.loads(self.registry_file.read_text(encoding="utf-8"),
                               object_pairs_hook=no_duplicate_keys)
        except FileNotFoundError:
            return self._empty()
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise PackError(f"pack registry: {error}") from error
        require(type(value) is dict and set(value) == {"version", "packs"}
                and value["version"] == REGISTRY_VERSION and type(value["packs"]) is dict,
                "pack registry: unsupported or malformed")
        for pack_id, record in value["packs"].items():
            require(type(pack_id) is str and ID.fullmatch(pack_id),
                    "pack registry: invalid pack id")
            require(type(record) is dict
                    and set(record) == {"versions", "selected", "enabled", "history", "grants", "config"}
                    and type(record["versions"]) is dict
                    and (record["selected"] is None or type(record["selected"]) is str)
                    and type(record["enabled"]) is bool
                    and type(record["history"]) is list
                    and all(type(item) is str for item in record["history"])
                    and (record["grants"] is None or type(record["grants"]) is dict)
                    and type(record["config"]) is dict,
                    f"pack registry: malformed entry for {pack_id}")
            require(record["selected"] is None or record["selected"] in record["versions"],
                    f"pack registry: selected version is not installed for {pack_id}")
            require(all(item in record["versions"] for item in record["history"]),
                    f"pack registry: rollback version is not installed for {pack_id}")
            if record["grants"] is not None:
                grants = record["grants"]
                require(set(grants) == {"origins", "capabilities", "dependencies"}
                        and type(grants["origins"]) is list
                        and type(grants["capabilities"]) is list
                        and all(type(item) is str for item in grants["origins"] + grants["capabilities"])
                        and type(grants["dependencies"]) is dict
                        and all(type(name) is str and type(version) is str
                                for name, version in grants["dependencies"].items()),
                        f"pack registry: malformed grants for {pack_id}")
            require(not record["enabled"] or (record["selected"] is not None
                                               and record["grants"] is not None),
                    f"pack registry: enabled pack lacks version or grants for {pack_id}")
            for version, metadata in record["versions"].items():
                require(type(version) is str and VERSION.fullmatch(version)
                        and type(metadata) is dict and set(metadata) == {"hashes"}
                        and type(metadata["hashes"]) is dict
                        and all(type(name) is str and type(digest) is str
                                and len(digest) == 64
                                for name, digest in metadata["hashes"].items()),
                        f"pack registry: malformed version for {pack_id}")
        return value

    def save(self, value):
        _private_directory(self.registry_file.parent)
        fd, name = tempfile.mkstemp(prefix=".pack-registry-", dir=self.registry_file.parent)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(value, handle, indent=2, sort_keys=True)
                handle.write("\n")
            temporary.chmod(0o600)
            temporary.replace(self.registry_file)
        finally:
            temporary.unlink(missing_ok=True)

    def version_root(self, pack_id, version):
        return self.code / pack_id / "versions" / version

    def _record(self, registry, pack_id):
        record = registry["packs"].get(pack_id)
        require(type(record) is dict, f"pack is not installed: {pack_id}")
        return record

    def install(self, artifact):
        _private_directory(self.code)
        stage = Path(tempfile.mkdtemp(prefix=".stage-", dir=self.code))
        try:
            manifest = _extract_artifact(artifact, stage)
            hashes = _tree_hashes(stage, manifest)
            target = self.version_root(manifest["id"], manifest["version"])
            registry = self.load()
            record = registry["packs"].setdefault(manifest["id"], {
                "versions": {}, "selected": None, "enabled": False,
                "history": [], "grants": None, "config": {}})
            if target.exists():
                require(not target.is_symlink(), "installed pack version must not be a symlink")
                existing = load_manifest(target)
                require(_tree_hashes(target, existing) == hashes,
                        "installed version is immutable and has different content")
            else:
                _private_directory(target.parent)
                stage.replace(target)
            record["versions"][manifest["version"]] = {"hashes": hashes}
            self.save(registry)
            return {"id": manifest["id"], "version": manifest["version"],
                    "installed": True, "enabled": record["enabled"], "code": str(target)}
        finally:
            if stage.exists():
                shutil.rmtree(stage)

    def verify(self, registry, pack_id, version):
        record = self._record(registry, pack_id)
        metadata = record["versions"].get(version)
        require(type(metadata) is dict and type(metadata.get("hashes")) is dict,
                f"pack version is not installed: {pack_id}@{version}")
        root = self.version_root(pack_id, version)
        require(root.is_dir() and not root.is_symlink(),
                f"installed pack code is missing: {pack_id}@{version}")
        manifest = load_manifest(root)
        require(manifest["id"] == pack_id and manifest["version"] == version,
                "installed pack identity does not match its registry path")
        require(_tree_hashes(root, manifest) == metadata["hashes"],
                f"installed pack integrity check failed: {pack_id}@{version}")
        return root, manifest

    def enable(self, pack_id, version=None, *, origins=(), capabilities=(), dependencies=None,
               config=None):
        registry = self.load()
        record = self._record(registry, pack_id)
        if version is None:
            version = record["selected"]
        require(type(version) is str, "enable requires an installed --version")
        root, manifest = self.verify(registry, pack_id, version)
        check_activation(manifest, origins, capabilities, dependencies or {})
        unsupported = set(manifest["entrypoints"]) - {"page"}
        page = manifest["entrypoints"].get("page")
        require(not unsupported and page is not None
                and page["interface"] == "browser-scripts-v1",
                "installed host currently binds only page/browser-scripts-v1 packs")
        overrides = resolve_config(manifest, config)
        try:
            profile = json.loads((self.root / "profile.json").read_text(encoding="utf-8"),
                                 object_pairs_hook=no_duplicate_keys)
            base = profile.get("bridge")
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise PackError(f"profile configuration: {error}") from error
        from .bridge import configuration
        base = configuration(base)
        require(base["enabled"], "installed page packs require an enabled profile bridge")
        claimed = {}
        page_count = len(base["page_scripts"]) + len(page["files"])
        origins_count = len(set(base["allow_origins"]) | set(manifest["access"]["origins"]))
        for other_id, other in registry["packs"].items():
            if other_id == pack_id or not other["enabled"]:
                continue
            _, other_manifest = self.verify(registry, other_id, other["selected"])
            other_page = other_manifest["entrypoints"].get("page")
            if other_page and other_page["interface"] == "browser-scripts-v1":
                page_count += len(other_page["files"])
                origins_count += len(set(other_manifest["access"]["origins"]) - set(base["allow_origins"]))
                for origin in other_manifest["access"]["origins"]:
                    claimed[origin] = other_id
        conflict = set(manifest["access"]["origins"]) & set(claimed)
        require(not conflict,
                f"page origins already claimed by another pack: {sorted(conflict)}")
        require(page_count <= 64 and origins_count <= 64,
                "enabled packs exceed the bridge limit of 64 origins/scripts")
        old = record["selected"] if record["enabled"] else None
        if old and old != version:
            record["history"].append(old)
        record.update(selected=version, enabled=True,
                      grants={"origins": sorted(set(origins)),
                              "capabilities": sorted(set(capabilities)),
                              "dependencies": dict(dependencies or {})},
                      config=overrides)
        self.save(registry)
        return {"id": pack_id, "version": version, "enabled": True,
                "code": str(root), "applies": "next profile on"}

    def update(self, artifact):
        before = self.load()
        installed = self.install(artifact)
        record = before["packs"].get(installed["id"])
        if record and record["enabled"]:
            grants = record["grants"]
            return self.enable(installed["id"], installed["version"],
                               origins=grants["origins"], capabilities=grants["capabilities"],
                               dependencies=grants["dependencies"], config=record["config"])
        return installed

    def rollback(self, pack_id):
        registry = self.load()
        record = self._record(registry, pack_id)
        require(record["enabled"], "rollback requires an enabled pack")
        require(bool(record["history"]), "rollback has no previous active version")
        version = record["history"][-1]
        self.verify(registry, pack_id, version)
        grants = record["grants"]
        root, manifest = self.verify(registry, pack_id, version)
        check_activation(manifest, grants["origins"], grants["capabilities"], grants["dependencies"])
        resolve_config(manifest, record["config"])
        record["history"].pop()
        record["selected"] = version
        self.save(registry)
        return {"id": pack_id, "version": version, "enabled": True,
                "code": str(root), "applies": "next profile on"}

    def disable(self, pack_id):
        registry = self.load()
        record = self._record(registry, pack_id)
        record["enabled"] = False
        self.save(registry)
        return {"id": pack_id, "version": record["selected"], "enabled": False,
                "applies": "next profile on; reload open pages to remove executed UI"}

    def uninstall(self, pack_id, version=None):
        registry = self.load()
        record = self._record(registry, pack_id)
        require(not record["enabled"], "disable pack before uninstall")
        versions = list(record["versions"]) if version is None else [version]
        for selected in versions:
            self.verify(registry, pack_id, selected)
        for selected in versions:
            target = self.version_root(pack_id, selected)
            require(target.parent.parent == self.code / pack_id,
                    "refusing to remove an unexpected pack path")
            shutil.rmtree(target)
            record["versions"].pop(selected)
        record["history"] = [item for item in record["history"] if item in record["versions"]]
        if record["selected"] not in record["versions"]:
            record["selected"] = None
        if not record["versions"]:
            registry["packs"].pop(pack_id)
        self.save(registry)
        return {"id": pack_id, "removed_versions": versions, "code_removed": True,
                "state_retained": True, "data_retained": True, "logs_retained": True}

    def status(self):
        return self.load()

    def effective_bridge(self, base):
        registry = self.load()
        enabled = []
        claimed = {}
        for pack_id in sorted(registry["packs"]):
            record = registry["packs"][pack_id]
            if not record["enabled"]:
                continue
            root, manifest = self.verify(registry, pack_id, record["selected"])
            grants = record["grants"]
            check_activation(manifest, grants["origins"], grants["capabilities"], grants["dependencies"])
            page = manifest["entrypoints"].get("page")
            require(set(manifest["entrypoints"]) == {"page"} and page["interface"] == "browser-scripts-v1",
                    "enabled pack has no installed host binding")
            for origin in manifest["access"]["origins"]:
                require(origin not in claimed,
                        f"page origin {origin} is claimed by both {claimed.get(origin)} and {pack_id}")
                claimed[origin] = pack_id
            enabled.append((pack_id, root, manifest, page))
        if not enabled:
            return base
        require(base is not None and base.get("enabled") is True,
                "enabled page packs require an enabled profile bridge")
        result = json.loads(json.dumps(base))
        for _, root, manifest, page in enabled:
            for origin in manifest["access"]["origins"]:
                if origin not in result["allow_origins"]:
                    result["allow_origins"].append(origin)
            result["page_scripts"].extend(str(root / name) for name in page["files"])
        from .bridge import configuration
        return configuration(result)


def _parse_dependency(values):
    result = {}
    for value in values:
        pack_id, separator, version = value.partition("=")
        require(separator and pack_id and version, "dependency grants use id=version")
        require(pack_id not in result, f"duplicate dependency grant: {pack_id}")
        result[pack_id] = version
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build")
    build.add_argument("source", type=Path)
    build.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = build_artifact(args.source, args.output)
    except (PackError, OSError, ValueError) as error:
        parser.exit(1, f"tap pack: {error}\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
