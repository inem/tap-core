"""Resolve a GitHub release into the existing immutable PackStore."""

import json
from pathlib import Path
import re
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .pack_store import MAX_ARCHIVE_BYTES, PackStore, _digest, _extract_artifact
from .packs import PackError, VERSION, require


PART = r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,98}[A-Za-z0-9])?"
SOURCE = re.compile(rf"({PART})/({PART})(?:@v?([0-9]+\.[0-9]+\.[0-9]+))?\Z")


def _request_json(url, opener):
    request = Request(url, headers={"Accept": "application/vnd.github+json",
                                    "User-Agent": "tap-core-pack-add/1"})
    try:
        with opener(request, timeout=30) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
            require(len(raw) <= 2 * 1024 * 1024, "GitHub releases response exceeds 2 MiB")
            return json.loads(raw)
    except (HTTPError, URLError, OSError, ValueError) as error:
        raise PackError(f"cannot read GitHub releases: {error}") from error


def resolve(spec, opener=urlopen):
    match = SOURCE.fullmatch(spec)
    require(match is not None, "source must be OWNER/REPO or OWNER/REPO@MAJOR.MINOR.PATCH")
    owner, repository, requested = match.groups()
    api = f"https://api.github.com/repos/{owner}/{repository}/releases?per_page=100"
    releases = _request_json(api, opener)
    require(type(releases) is list, "GitHub releases response is malformed")
    candidates = []
    for release in releases:
        if type(release) is not dict or release.get("draft") is True:
            continue
        tag = release.get("tag_name")
        version = tag[1:] if isinstance(tag, str) and tag.startswith("v") else tag
        if not isinstance(version, str) or VERSION.fullmatch(version) is None:
            continue
        if requested is None and release.get("prerelease") is True:
            continue
        if requested is not None and version != requested:
            continue
        candidates.append((tuple(map(int, version.split("."))), version, release))
    require(bool(candidates),
            f"no {'stable ' if requested is None else ''}release found for {owner}/{repository}"
            + (f"@{requested}" if requested else ""))
    _key, version, release = max(candidates, key=lambda item: item[0])
    assets = [asset for asset in release.get("assets", [])
              if isinstance(asset, dict) and isinstance(asset.get("name"), str)
              and asset["name"].endswith(f"-{version}.tap-pack")
              and isinstance(asset.get("browser_download_url"), str)]
    require(len(assets) == 1,
            f"release {version} must contain exactly one *-{version}.tap-pack asset")
    asset_url = assets[0]["browser_download_url"]
    location = urlsplit(asset_url)
    require(location.scheme == "https" and location.hostname in {
        "github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com"},
        "release asset URL is not on an allowed GitHub host")
    return {"owner": owner, "repository": repository, "version": version,
            "source": f"github:{owner}/{repository}@{version}",
            "url": asset_url}


def _download(resolved, destination, opener):
    request = Request(resolved["url"], headers={"User-Agent": "tap-core-pack-add/1"})
    try:
        with opener(request, timeout=60) as response, Path(destination).open("xb") as output:
            total = 0
            while True:
                block = response.read(1024 * 1024)
                if not block:
                    break
                total += len(block)
                require(total <= MAX_ARCHIVE_BYTES, "downloaded artifact exceeds 32 MiB")
                output.write(block)
    except (HTTPError, URLError, OSError) as error:
        raise PackError(f"cannot download pack artifact: {error}") from error


def add(profile_root, spec, *, assume_yes=False, opener=urlopen, input_fn=input, output_fn=print):
    resolved = resolve(spec, opener)
    with tempfile.TemporaryDirectory(prefix="tap-pack-add-") as directory:
        artifact = Path(directory) / "pack.tap-pack"
        _download(resolved, artifact, opener)
        inspect = Path(directory) / "inspect"
        inspect.mkdir()
        manifest = _extract_artifact(artifact, inspect)
        require(manifest["version"] == resolved["version"],
                "release tag and pack manifest versions differ")
        commands = manifest["entrypoints"].get("command", {}).get("commands", [])
        store = PackStore(profile_root)
        record = store.load()["packs"].get(manifest["id"])
        metadata = (record or {}).get("versions", {}).get(manifest["version"], {})
        requested_grants = {"origins": sorted(manifest["access"]["origins"]),
                            "capabilities": sorted(manifest["access"]["capabilities"]),
                            "dependencies": {item["id"]: item["version"]
                                             for item in manifest["requires"]["dependencies"]}}
        if (record and record.get("enabled") and record.get("selected") == manifest["version"]
                and record.get("grants") == requested_grants
                and metadata.get("artifact_sha256") == _digest(artifact)
                and metadata.get("source") == resolved["source"]):
            return {"id": manifest["id"], "version": manifest["version"],
                    "enabled": True, "already_configured": True,
                    "source": resolved["source"],
                    "next": "tap " + " ".join(commands[0]["path"]) + " --help" if commands else None}
        summary = commands[0]["summary"] if commands else ", ".join(manifest["entrypoints"])
        output_fn(f"Add {manifest['id']} by {resolved['owner']} ({manifest['version']})")
        output_fn("Purpose: " + summary)
        output_fn("Requested sites: " + ", ".join(manifest["access"]["origins"]))
        output_fn("Requested capabilities: " + ", ".join(manifest["access"]["capabilities"]))
        output_fn("This installs local executable code; access grants are not a sandbox.")
        if not assume_yes:
            answer = input_fn("Continue? [y/N] ").strip().lower()
            require(answer in ("y", "yes"), "cancelled; pack was not installed or enabled")
        dependencies = requested_grants["dependencies"]
        installed = store.install(artifact, source=resolved["source"])
        enabled = store.enable(manifest["id"], manifest["version"],
                               origins=manifest["access"]["origins"],
                               capabilities=manifest["access"]["capabilities"],
                               dependencies=dependencies)
        return {**installed, **enabled, "source": resolved["source"],
                "next": "tap " + " ".join(commands[0]["path"]) + " --help" if commands else None}
