"""Profile-local controller for opaque commands exposed by page packs."""
import json
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .runtime import TapError


def allow(profile, origin: str):
    """Persist one same-user development origin and refresh the live snapshot."""
    from .bridge import exact_origin
    try:
        origin = exact_origin(origin)
    except ValueError as error:
        raise TapError(str(error)) from error
    bridge = profile.bridge
    if not bridge or not bridge.get("enabled"):
        raise TapError("Development channel requires an enabled bridge")
    from .bridge import development_configuration
    from .pack_store import PackStore
    from .runtime import atomic_json
    development = development_configuration(profile.root)
    tools = list(development["tools"])
    registry = PackStore(profile.root).load()
    inspector = registry.get("packs", {}).get("tap.inspector")
    if inspector and inspector.get("enabled") and "tap.inspector" not in tools:
        tools.append("tap.inspector")
    changed = (origin not in bridge["allow_origins"] or origin in bridge["exclude_origins"]
               or origin not in development["origins"] or tools != development["tools"])
    if origin not in bridge["allow_origins"]:
        bridge["allow_origins"].append(origin)
    if origin in bridge["exclude_origins"]:
        bridge["exclude_origins"].remove(origin)
    if changed:
        origins = list(development["origins"])
        if origin not in origins:
            origins.append(origin)
        atomic_json(profile.root / "state/development.json",
                    {"version": 1, "origins": origins, "tools": tools})
        profile.save()
    return {
        "origin": origin,
        "allowed": True,
        "changed": changed,
        "applies": "immediately to Hub authorization; reload an already-open page for bootstrap injection",
        "mode": "development",
        "tools": tools,
        "pack_grants": "unchanged; named development tools receive a local page-only binding",
    }


def _runtime(root: Path):
    try:
        effective = json.loads((root / "state/effective-runtime.json").read_text())
    except (FileNotFoundError, ValueError):
        effective = None
    try:
        profile = json.loads((root / "profile.json").read_text())
        token = (root / "state/component-token").read_text().strip()
    except (OSError, ValueError) as error:
        raise TapError(f"Page controller is unavailable: {error}") from error
    bridge = (effective or {}).get("bridge") or profile.get("bridge")
    if not bridge or type(bridge.get("hub_port")) is not int:
        raise TapError("Page controller is unavailable: no local bridge")
    return f"http://127.0.0.1:{bridge['hub_port']}", token


def request(root: Path, method: str, path: str, body=None):
    base, token = _runtime(root)
    data = None if body is None else json.dumps(body).encode()
    call = Request(base + path, data=data, method=method, headers={
        "authorization": "Bearer " + token,
        "content-type": "application/json",
    })
    try:
        with urlopen(call, timeout=9) as response:
            return json.loads(response.read())
    except HTTPError as error:
        try:
            payload = json.loads(error.read())
            code = payload.get("error", {}).get("code")
        except (ValueError, AttributeError):
            code = None
        raise TapError(f"Page command failed: {code or error.code}") from error
    except (URLError, OSError, ValueError) as error:
        raise TapError(f"Page controller is unavailable: {error}") from error


def pages(root: Path):
    return request(root, "GET", "/v1/pages")


def call(root: Path, page: str, operation: str, args):
    from urllib.parse import quote
    return request(root, "POST", f"/v1/pages/{quote(page, safe='')}/commands",
                   {"operation": operation, "args": args})


def inspect(root: Path, page: str, selector: str, limit: int):
    return call(root, page, "tap.dev.inspect", {"selector": selector, "limit": limit})


def execute(root: Path, page: str, source: str):
    return call(root, page, "tap.dev.execute", {"source": source})
