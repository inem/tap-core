"""`tap capture reload`: hand the running proxy a new retention policy (#247).

Host effects only: compute the effective policy exactly as `tap on` does
(profile `capture` plus enabled pack request-body declarations), validate it
with the capture validator, write `state/capture-policy.json` atomically and
wait for the proxy's acknowledgement in `state/capture.json`. The proxy-side
swap lives in `capture.PolicyWatcher`. Routing, ports, passthrough and capture
size bounds stay restart-only; a pending difference there is reported, never
applied.
"""
import json
import os
import plistlib
import time

from .runtime import TapError, profile_lock

RESULT_VERSION = 1


def running_limits(profile):
    """Capture limits the running proxy was started with, or None if unknown."""
    from .capture import capture_limits
    try:
        plist = plistlib.loads(profile.plist.read_bytes())
        return capture_limits(json.loads(plist["EnvironmentVariables"]["TAP_CORE_CAPTURE"]))
    except (OSError, KeyError, TypeError, ValueError, plistlib.InvalidFileException):
        return None


def policy_state(profile, pid):
    """The `policy` block from the current proxy's health record, if any."""
    try:
        state = json.loads((profile.root / "state/capture.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if type(state) is not dict or state.get("pid") != pid or type(state.get("policy")) is not dict:
        return None
    return state["policy"]


def write_policy(path, document):
    temporary = path.with_name(path.name + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(document, indent=2) + "\n")
    temporary.replace(path)


def reload(profile, adapter, *, wait_seconds=5.0, poll=0.1):
    from .capture import (DEFAULT_CAPTURE, POLICY_FILE, RELOADABLE_KEYS, apply_policy_document,
                          capture_limits, policy_document)
    from .pack_store import PackStore
    with profile_lock(profile.root, wait_seconds=5):
        desired = PackStore(profile.root).effective_capture(capture_limits(profile.capture))
        document = policy_document(desired)
        revision = document["revision"]
        result = {"version": RESULT_VERSION, "revision": revision,
                  **{key: document[key] for key in RELOADABLE_KEYS}}
        pid = adapter.service_pid(profile)
        if not pid:
            # Nothing to hand over: the next `on` starts from the profile anyway.
            return dict(result, applied=False, applies="next on", restart_required=[])
        started = running_limits(profile)
        restart_required = sorted(key for key in DEFAULT_CAPTURE
                                  if started is not None and started[key] != desired[key])
        # Validate exactly as the proxy will before handing anything over.
        apply_policy_document(document, started or desired)
        before = policy_state(profile, pid)
        if before and before.get("revision") == revision and not before.get("error"):
            return dict(result, applied=True, applies="live", unchanged=True,
                        previous=revision, restart_required=restart_required)
        write_policy(profile.root / "state" / POLICY_FILE, document)
    deadline = time.monotonic() + wait_seconds
    while True:
        state = policy_state(profile, pid)
        if state and state.get("revision") == revision and state.get("source") == "reload":
            return dict(result, applied=True, applies="live", unchanged=False,
                        previous=(before or {}).get("revision"),
                        restart_required=restart_required)
        if state and state.get("rejected_revision") == revision:
            raise TapError(f"Running proxy rejected capture policy {revision}: {state.get('error')}")
        if time.monotonic() >= deadline:
            raise TapError(
                f"Capture policy {revision} was written but the running proxy did not acknowledge "
                f"it within {wait_seconds:g}s; its capture addon may predate live policy reload. "
                f"See {profile.root / 'logs/capture.log'}; tap off/on applies it")
        time.sleep(poll)
