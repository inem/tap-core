"""Profile-local controller for opaque commands exposed by page packs."""
import json
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .runtime import TapError


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
