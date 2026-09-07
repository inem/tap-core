"""Routing choices over the existing proxy backend, independent of capture.

These are internal host adapters, not an installed-pack API or an execution
sandbox. Local Capture remains unavailable until its separate acceptance.
"""
from contextlib import nullcontext
import json
from pathlib import Path

from .runtime import TapError, atomic_json, profile_lock


class ExplicitProxyRouting:
    """Clients opt into our listener; no system proxy mutation is owned here."""
    manages_system_settings = False
    recovered_message = "system proxy settings were not changed"
    off_message = "OFF — profile service stopped; explicit clients must stop using its proxy endpoint"

    def __init__(self, profile, os_adapter):
        self.profile, self.os = profile, os_adapter

    def backend_args(self):
        return ["--listen-host", "127.0.0.1", "-p", str(self.profile.port)]

    def capabilities(self):
        # Describes implemented capabilities, not observed permissions/readiness.
        return {"version": 1, "mechanism": "http_proxy", "configuration": self.profile.routing,
                "manages_system_settings": self.manages_system_settings,
                "process_selection": False, "process_attribution": False,
                "network_extension_required": False}

    def mutation_lock(self):
        return nullcontext()

    def enable(self):
        """Explicit clients configure their endpoint outside TAP."""

    def restore(self):
        """TAP owns no external routing settings in this configuration."""

    def recovery_pending(self):
        return self.profile.snapshot.exists()

    def verified(self):
        return "not_used"

    def probe(self):
        return self.os.flows(self.profile)

    def on_message(self):
        return f"ON — explicit proxy 127.0.0.1:{self.profile.port}; system settings unchanged"


class SystemProxyRouting(ExplicitProxyRouting):
    """Owns snapshot/restore of supported macOS HTTP(S) proxy settings."""
    manages_system_settings = True
    recovered_message = "previous proxy routing restored"
    off_message = "OFF — previous proxy routing restored, profile service stopped"

    def mutation_lock(self):
        shared = Path.home() / "Library/Application Support/TAP Core/network-control"
        return profile_lock(shared)

    def on_message(self):
        return "ON — HTTP and HTTPS proxy settings verified on all enabled services; traffic probe passed"

    def matches(self, actual, expected):
        # Disabled endpoints are retained to avoid briefly enabling them while
        # restoring a server address. This verifies routing, not every preference.
        return actual["enabled"] == expected["enabled"] and (
            not expected["enabled"] or (actual["server"], actual["port"]) == (expected["server"], expected["port"]))

    def verified(self):
        profile = self.profile
        expected = {"enabled": True, "server": "127.0.0.1", "port": profile.port}
        return all(self.matches(self.os.proxy(service, secure), expected)
                   for service in self.os.services() for secure in (False, True))

    def bypasses_match(self, before):
        if set(self.os.services()) != set(before):
            return False
        return all(self.os.bypass(service) == list(dict.fromkeys([*state["bypass"], "localhost", "127.0.0.1", "*.local"]))
                   for service, state in before.items())

    def enable(self):
        profile = self.profile
        if profile.snapshot.exists():
            before = json.loads(profile.snapshot.read_text())
            if self.verified() and self.bypasses_match(before):
                return
            raise TapError("Saved network state needs recovery; run off before enabling again")
        before = self.os.network_state()
        if any(state[kind]["enabled"] for state in before.values() for kind in ("http", "https")):
            raise TapError("An existing system proxy is enabled; refusing to replace another installation")
        atomic_json(profile.snapshot, before)  # persist before the first mutation
        expected = {"enabled": True, "server": "127.0.0.1", "port": profile.port}
        for service, state in before.items():
            self.os.set_proxy(service, False, expected)
            self.os.set_proxy(service, True, expected)
            self.os.set_bypass(service, list(dict.fromkeys([*state["bypass"], "localhost", "127.0.0.1", "*.local"])))
        if not self.verified():
            raise TapError("Could not verify both proxies on every network service")
        if not self.bypasses_match(before):
            raise TapError("Could not verify bypass domains on every service")

    def restore(self):
        profile = self.profile
        if not profile.snapshot.exists():
            # No ownership journal: never modify someone else's routing.
            for service in self.os.services():
                for secure in (False, True):
                    state = self.os.proxy(service, secure)
                    if state["enabled"] and state["server"] in ("127.0.0.1", "localhost") and state["port"] == profile.port:
                        raise TapError("Profile proxy is enabled but its recovery snapshot is missing; refusing to stop")
            return
        before = json.loads(profile.snapshot.read_text())
        errors = []
        owned = {"enabled": True, "server": "127.0.0.1", "port": profile.port}
        # Do not overwrite an unrelated proxy enabled after we took the snapshot.
        for service in before:
            for secure in (False, True):
                current = self.os.proxy(service, secure)
                if current["enabled"] and not self.matches(current, owned):
                    raise TapError(f"Proxy changed outside this profile: {service}; recovery needs inspection")
        for service, state in before.items():
            for secure, kind in ((False, "http"), (True, "https")):
                try:
                    self.os.set_proxy(service, secure, state[kind])
                except TapError as error:
                    errors.append(str(error))
            try:
                self.os.set_bypass(service, state["bypass"])
            except TapError as error:
                errors.append(str(error))
        for service, state in before.items():
            if not all(self.matches(self.os.proxy(service, secure), state[kind]) for secure, kind in ((False, "http"), (True, "https"))):
                errors.append(f"Proxy restoration not verified: {service}")
            if self.os.bypass(service) != state["bypass"]:
                errors.append(f"Bypass restoration not verified: {service}")
        for service in self.os.services():
            if service not in before and any(self.matches(self.os.proxy(service, secure), owned) for secure in (False, True)):
                errors.append(f"New service points to this profile without a recovery snapshot: {service}")
        if errors:
            raise TapError("Network recovery incomplete; service kept running: " + "; ".join(errors))
        profile.snapshot.unlink()


def select_routing(profile, os_adapter):
    if profile.routing == "explicit":
        return ExplicitProxyRouting(profile, os_adapter)
    if profile.routing == "system":
        return SystemProxyRouting(profile, os_adapter)
    raise TapError(f"Unsupported routing mode: {profile.routing}; no fallback was applied")
