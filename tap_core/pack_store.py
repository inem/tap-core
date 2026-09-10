"""Immutable pack artifacts and profile-local activation state.

Installed host bindings: page/browser-scripts-v1, reader/python-jsonl-v1,
handler/python-jsonl-v1 and command/process-argv-v1. Other manifest roles remain
valid but cannot be enabled until their host bindings exist. No second pack store.
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
HOST_ROLES = frozenset({"page", "reader", "handler", "command"})
PAGE_APPLIES = ("immediately for new documents; already bootstrapped pages reconcile "
                "the plan and reload classic scripts; documents loaded before the Core "
                "bootstrap existed require one reload")
LIVE_PACK_APPLIES = PAGE_APPLIES + "; reader/handler bindings still require profile on"


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
                with source:
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
        self.resources = self.root / "resources"
        self.registry_file = self.root / "state/pack-registry.json"

    def _safe_profile_path(self, path):
        """Reject symlinks in every existing component below the profile root."""
        path = Path(path)
        try:
            relative = path.relative_to(self.root)
        except ValueError as error:
            raise PackError(f"Pack path escapes the profile: {path}") from error
        current = self.root
        for part in relative.parts:
            current = current / part
            require(not current.is_symlink(),
                    f"Pack path must not traverse a symlink: {current}")
        return path

    def _empty(self):
        return {"version": REGISTRY_VERSION, "packs": {}}

    def load(self):
        self._safe_profile_path(self.registry_file)
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
                        and type(metadata) is dict
                        and set(metadata) <= {"hashes", "artifact_sha256", "source"}
                        and "hashes" in metadata
                        and type(metadata["hashes"]) is dict
                        and all(type(name) is str and type(digest) is str
                                and len(digest) == 64
                                for name, digest in metadata["hashes"].items())
                        and ("artifact_sha256" not in metadata
                             or (type(metadata["artifact_sha256"]) is str
                                 and len(metadata["artifact_sha256"]) == 64))
                        and ("source" not in metadata or type(metadata["source"]) is str),
                        f"pack registry: malformed version for {pack_id}")
        return value

    def save(self, value):
        _private_directory(self._safe_profile_path(self.registry_file.parent))
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
        return self._safe_profile_path(self.code / pack_id / "versions" / version)

    def resource_file(self, resource):
        return self._safe_profile_path(
            self.resources / "page" / resource["id"] / resource["version"]
            / (resource["sha256"] + ".js"))

    def _materialize_resources(self, source, manifest):
        for resource in manifest.get("resources", []):
            directory = self.resources
            for name in ("page", resource["id"], resource["version"]):
                directory = _private_directory(self._safe_profile_path(directory / name))
            target = self.resource_file(resource)
            if target.exists():
                require(target.is_file() and not target.is_symlink()
                        and _digest(target) == resource["sha256"],
                        f"shared page resource is changed or unsafe: "
                        f"{resource['id']}@{resource['version']}")
                continue
            fd, name = tempfile.mkstemp(prefix=".resource-", dir=directory)
            temporary = Path(name)
            try:
                with os.fdopen(fd, "wb") as output, (source / resource["file"]).open("rb") as input_:
                    shutil.copyfileobj(input_, output)
                temporary.chmod(0o600)
                require(_digest(temporary) == resource["sha256"],
                        f"resource changed while installing: {resource['id']}@{resource['version']}")
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)

    def _record(self, registry, pack_id):
        record = registry["packs"].get(pack_id)
        require(type(record) is dict, f"pack is not installed: {pack_id}")
        return record

    def install(self, artifact, *, source=None):
        _private_directory(self._safe_profile_path(self.code))
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
            self._materialize_resources(stage, manifest)
            if not target.exists():
                _private_directory(self._safe_profile_path(target.parent))
                stage.replace(target)
            metadata = {"hashes": hashes, "artifact_sha256": _digest(artifact)}
            if source is not None:
                require(type(source) is str and bool(source), "pack source must be a nonempty string")
                metadata["source"] = source
            previous = record["versions"].get(manifest["version"])
            if previous and "source" in previous and "source" not in metadata:
                metadata["source"] = previous["source"]
            record["versions"][manifest["version"]] = metadata
            self.save(registry)
            return {"id": manifest["id"], "version": manifest["version"],
                    "installed": True, "enabled": record["enabled"], "code": str(target),
                    "resources": len(manifest.get("resources", []))}
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

    def _profile_bridge(self):
        try:
            profile = json.loads((self.root / "profile.json").read_text(encoding="utf-8"),
                                 object_pairs_hook=no_duplicate_keys)
            base = profile.get("bridge")
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise PackError(f"profile configuration: {error}") from error
        if base is None:
            return None
        from .bridge import configuration
        return configuration(base)

    def _profile_components(self):
        try:
            profile = json.loads((self.root / "profile.json").read_text(encoding="utf-8"),
                                 object_pairs_hook=no_duplicate_keys)
            return profile.get("components")
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise PackError(f"profile configuration: {error}") from error

    def _require_host_entrypoints(self, manifest):
        roles = set(manifest["entrypoints"])
        unsupported = roles - HOST_ROLES
        require(not unsupported,
                "installed host has no binding for: " + ", ".join(sorted(unsupported)))
        require(bool(roles), "pack has no entrypoints")
        page = manifest["entrypoints"].get("page")
        if page is not None:
            require(page.get("interface") == "browser-scripts-v1"
                    and set(page) == {"interface", "uses"},
                    "page entrypoint requires browser-scripts-v1 uses binding")
        for role in ("reader", "handler"):
            entry = manifest["entrypoints"].get(role)
            if entry is None:
                continue
            require(entry.get("interface") == "python-jsonl-v1"
                    and type(entry.get("file")) is str and entry["file"] in manifest["files"],
                    f"{role} entrypoint requires python-jsonl-v1 file binding")
        command = manifest["entrypoints"].get("command")
        if command is not None:
            require(command.get("interface") == "process-argv-v1"
                    and command.get("runtime") == "host-python"
                    and type(command.get("file")) is str
                    and command["file"] in manifest["files"],
                    "command entrypoint requires process-argv-v1 host-python binding")

    def _validate_commands(self, registry):
        from .commands import validate_external_command_paths
        manifests = [(pack_id, manifest)
                     for pack_id, _root, manifest, _record in self._enabled_packs(registry)]
        validate_external_command_paths(manifests)

    def _enabled_packs(self, registry):
        enabled = []
        for pack_id in sorted(registry["packs"]):
            record = registry["packs"][pack_id]
            if not record["enabled"]:
                continue
            root, manifest = self.verify(registry, pack_id, record["selected"])
            grants = record["grants"]
            check_activation(manifest, grants["origins"], grants["capabilities"],
                             grants["dependencies"])
            resolve_config(manifest, record["config"])
            self._require_host_entrypoints(manifest)
            enabled.append((pack_id, root, manifest, record))
        return enabled

    def _effective_bridge(self, registry, base):
        from .bridge import development_configuration
        development = development_configuration(self.root)
        development_origins = set(development["origins"])
        development_tools = set(development["tools"])
        enabled = self._enabled_packs(registry)
        page_packs = []
        origin_packs = []
        for pack_id, root, manifest, record in enabled:
            roles = set(manifest["entrypoints"])
            if "page" in roles:
                page_packs.append((pack_id, root, manifest, manifest["entrypoints"]["page"], record))
            if roles & {"page", "handler"}:
                origin_packs.append((pack_id, manifest))
        if not page_packs and not origin_packs:
            return base
        require(base is not None and base.get("enabled") is True,
                "enabled page/handler packs require an enabled profile bridge")

        result = json.loads(json.dumps(base))
        base_origins = list(result["allow_origins"])
        script_origins = [list(base_origins) for _ in result["page_scripts"]]
        page_pack_origins = []
        for pack_id, _root, manifest, _record in enabled:
            origins = list(manifest["access"]["origins"])
            if pack_id in development_tools and "page" in manifest["entrypoints"]:
                origins += sorted(development_origins - set(origins))
            page_pack_origins.append({
                "id": pack_id, "version": manifest["version"], "origins": origins,
                "features": json.loads(json.dumps(manifest.get("features", []))),
            })
        for _pack_id, manifest in origin_packs:
            for origin in manifest["access"]["origins"]:
                if origin not in result["allow_origins"]:
                    result["allow_origins"].append(origin)
        if not page_packs:
            from .bridge import configuration
            configuration(result, script_origins)
            result["page_script_origins"] = script_origins
            result["page_pack_origins"] = page_pack_origins
            return result

        declarations = {}
        use_orders = []
        for pack_id, root, manifest, page, record in page_packs:
            origins = list(manifest["access"]["origins"])
            if pack_id in development_tools:
                origins += sorted(development_origins - set(origins))
            use_orders.append({"pack": pack_id, "origins": tuple(origins),
                               "resources": tuple(use["id"] for use in page["uses"])})
            resources = {(resource["id"], resource["version"]): resource
                         for resource in manifest.get("resources", [])}
            for use in page["uses"]:
                resource = resources[(use["id"], use["version"])]
                shared = self.resource_file(resource)
                require(shared.is_file() and not shared.is_symlink()
                        and _digest(shared) == resource["sha256"],
                        f"shared page resource is missing or changed: "
                        f"{resource['id']}@{resource['version']}")
                resource_id = use["id"]
                declaration = {"version": use["version"],
                               "digest": resource["sha256"],
                               "path": str(shared),
                               "origins": set(origins),
                               "pack": pack_id}
                current = declarations.get(resource_id)
                if current is None:
                    declarations[resource_id] = declaration
                    continue
                require(current["version"] == declaration["version"],
                        f"page resource {resource_id} requests conflicting versions: "
                        f"{current['version']} and {declaration['version']}")
                require(current["digest"] == declaration["digest"],
                        f"page resource {resource_id}@{use['version']} has conflicting content "
                        f"in {current['pack']} and {pack_id}")
                current["origins"].update(origins)
        from .bridge import configuration, order_page_resources
        try:
            ordered = order_page_resources(declarations, use_orders)
        except ValueError as error:
            raise PackError(str(error)) from error
        for resource_id, origins in ordered:
            declaration = declarations[resource_id]
            result["page_scripts"].append(declaration["path"])
            script_origins.append(origins)
        configuration(result, script_origins)
        result["page_script_origins"] = script_origins
        result["page_pack_origins"] = page_pack_origins
        return result

    def _refuse_incompatible_reader_progress(self, name, spec):
        """Refuse pack activation that would strand an existing reader checkpoint."""
        from .readers import fingerprint, validate_definition
        validate_definition(spec)
        checkpoint = self.root / "state/readers" / name / "checkpoint.json"
        if not checkpoint.is_file():
            return
        require(not checkpoint.is_symlink(), f"reader checkpoint must not be a symlink: {name}")
        try:
            state = json.loads(checkpoint.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise PackError(f"reader {name!r} checkpoint unreadable: {error}") from error
        if state.get("definition") == fingerprint(spec):
            return
        raise PackError(
            f"reader {name!r} has progress under another definition; "
            f"run `tap reader replay {name}` with the new binding, or remove "
            f"{checkpoint}, then enable/update again — checkpoint was not reset"
        )

    def _effective_components(self, registry, base):
        """Project enabled pack reader/handler entrypoints onto managed components."""
        projected_readers = {}
        projected_handlers = {}
        for pack_id, root, manifest, record in self._enabled_packs(registry):
            config = resolve_config(manifest, record["config"])
            reader = manifest["entrypoints"].get("reader")
            if reader is not None:
                path = (root / reader["file"]).resolve()
                require(path.is_file() and not path.is_symlink() and path.parent == root.resolve(),
                        f"reader file missing or unsafe: {pack_id}")
                projected_readers[pack_id] = {
                    "version": 1,
                    "revision": f"{pack_id}@{manifest['version']}",
                    "command": [None, str(path)],  # python filled after base check
                    "config": config,
                }
            handler = manifest["entrypoints"].get("handler")
            if handler is not None:
                path = (root / handler["file"]).resolve()
                require(path.is_file() and not path.is_symlink() and path.parent == root.resolve(),
                        f"handler file missing or unsafe: {pack_id}")
                projected_handlers[pack_id] = {
                    "command": [None, str(path)],
                    "config": config,
                    "origins": list(manifest["access"]["origins"]),
                }
        if not projected_readers and not projected_handlers:
            return base
        require(base is not None and type(base) is dict
                and type(base.get("python")) is str and Path(base["python"]).is_absolute(),
                "enabled pack readers/handlers require profile components with absolute python")
        if projected_handlers:
            require(type(base.get("bun")) is str and Path(base["bun"]).is_absolute(),
                    "enabled pack handlers require absolute bun")
        else:
            require(type(base.get("bun")) is str,
                    "enabled pack readers require profile components with a bun string field")
        result = json.loads(json.dumps(base))
        python = result["python"]
        for name, spec in projected_readers.items():
            require(name not in result["readers"],
                    f"pack reader conflicts with configured reader name: {name}")
            spec["command"][0] = python
            result["readers"][name] = spec
        for name, spec in projected_handlers.items():
            require(name not in result["handlers"],
                    f"pack handler conflicts with configured handler name: {name}")
            spec["command"][0] = python
            result["handlers"][name] = spec
        return result

    def _refuse_live_component_change(self, before_registry, candidate):
        """Refuse before save when a running profile would change reader/handler bindings."""
        base = self._profile_components()
        if base is None:
            return
        before = self._effective_components(before_registry, base)
        after = self._effective_components(candidate, base)
        if before != after:
            raise PackError(
                'Stop this profile with off before changing reader/handler packs; '
                'page-only pack changes may apply while the proxy is running')

    def enable(self, pack_id, version=None, *, origins=(), capabilities=(), dependencies=None,
               config=None, live=False):
        registry = self.load()
        record = self._record(registry, pack_id)
        if version is None:
            version = record["selected"]
        require(type(version) is str, "enable requires an installed --version")
        root, manifest = self.verify(registry, pack_id, version)
        check_activation(manifest, origins, capabilities, dependencies or {})
        self._require_host_entrypoints(manifest)
        overrides = resolve_config(manifest, config)
        candidate = json.loads(json.dumps(registry))
        candidate_record = self._record(candidate, pack_id)
        old = candidate_record["selected"] if candidate_record["enabled"] else None
        if old and old != version:
            candidate_record["history"].append(old)
        candidate_record.update(selected=version, enabled=True,
                                grants={"origins": sorted(set(origins)),
                                        "capabilities": sorted(set(capabilities)),
                                        "dependencies": dict(dependencies or {})},
                                config=overrides)
        self._validate_commands(candidate)
        self._effective_bridge(candidate, self._profile_bridge())
        projected = self._effective_components(candidate, self._profile_components())
        if "reader" in manifest["entrypoints"] and projected is not None:
            self._refuse_incompatible_reader_progress(pack_id, projected["readers"][pack_id])
        if live:
            self._refuse_live_component_change(registry, candidate)
        self.save(candidate)
        roles = set(manifest["entrypoints"])
        if roles & {"reader", "handler"}:
            applies = "next profile on"
        elif "page" in roles:
            applies = PAGE_APPLIES
        elif "command" in roles:
            applies = "immediately for new command invocations"
        else:
            applies = "next profile on"
        if "command" in roles and "page" in roles and not (roles & {"reader", "handler"}):
            applies = "immediately for new command invocations; " + PAGE_APPLIES
        return {"id": pack_id, "version": version, "enabled": True,
                "code": str(root), "applies": applies}

    def update(self, artifact, *, live=False):
        before = self.load()
        installed = self.install(artifact)
        record = before["packs"].get(installed["id"])
        if record and record["enabled"]:
            grants = record["grants"]
            return self.enable(installed["id"], installed["version"],
                               origins=grants["origins"], capabilities=grants["capabilities"],
                               dependencies=grants["dependencies"], config=record["config"],
                               live=live)
        return installed

    def rollback(self, pack_id, *, live=False):
        registry = self.load()
        record = self._record(registry, pack_id)
        require(record["enabled"], "rollback requires an enabled pack")
        require(bool(record["history"]), "rollback has no previous active version")
        version = record["history"][-1]
        root, manifest = self.verify(registry, pack_id, version)
        grants = record["grants"]
        check_activation(manifest, grants["origins"], grants["capabilities"], grants["dependencies"])
        resolve_config(manifest, record["config"])
        self._require_host_entrypoints(manifest)
        candidate = json.loads(json.dumps(registry))
        candidate_record = self._record(candidate, pack_id)
        candidate_record["history"].pop()
        candidate_record["selected"] = version
        self._validate_commands(candidate)
        self._effective_bridge(candidate, self._profile_bridge())
        projected = self._effective_components(candidate, self._profile_components())
        if "reader" in manifest["entrypoints"] and projected is not None:
            self._refuse_incompatible_reader_progress(pack_id, projected["readers"][pack_id])
        if live:
            self._refuse_live_component_change(registry, candidate)
        self.save(candidate)
        roles = set(manifest["entrypoints"])
        if roles & {"reader", "handler"}:
            applies = "next profile on"
        elif "page" in roles:
            applies = PAGE_APPLIES
        elif "command" in roles:
            applies = "immediately for new command invocations"
        else:
            applies = "next profile on"
        if "command" in roles and "page" in roles and not (roles & {"reader", "handler"}):
            applies = "immediately for new command invocations; " + PAGE_APPLIES
        return {"id": pack_id, "version": version, "enabled": True,
                "code": str(root), "applies": applies}

    def disable(self, pack_id, *, live=False):
        registry = self.load()
        before = json.loads(json.dumps(registry))
        record = self._record(registry, pack_id)
        record["enabled"] = False
        if live:
            self._refuse_live_component_change(before, registry)
        self.save(registry)
        return {"id": pack_id, "version": record["selected"], "enabled": False,
                "applies": "immediately for command discovery; service/page bindings apply next profile on; "
                           "reload open pages to remove executed UI"}

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
                "shared_resources_retained": True,
                "state_retained": True, "data_retained": True, "logs_retained": True}

    def status(self):
        return self.load()

    def effective_bridge(self, base):
        return self._effective_bridge(self.load(), base)

    def effective_components(self, base):
        return self._effective_components(self.load(), base)


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
