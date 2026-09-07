"""Existing TAP lifecycle with explicit profile paths and macOS operations.

Preserves launchd KeepAlive, raised fd limit, start/arm/probe, and
disarm-before-stop. See docs/runtime.md for deliberate failure-path changes.
"""
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shlex
import socket
import subprocess
import time
from urllib.parse import urlsplit

BACKEND_VERSION = "12.2.3"
ADDON = Path(__file__).with_name("capture.py").resolve()
NS = "/usr/sbin/networksetup"


class TapError(Exception):
    pass


class StartupError(TapError):
    """Startup mutated the profile job; Lifecycle must recover before cleanup."""


def atomic_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)


@dataclass
class Profile:
    root: Path
    backend: str
    port: int
    routing: str
    probe_url: str
    addons: list
    version: int = 1

    def __post_init__(self):
        self.root = self.root.expanduser().resolve()
        if self.version != 1 or self.routing not in ("explicit", "system"):
            raise TapError("Unsupported profile version or routing mode")
        if type(self.port) is not int or not 1024 <= self.port <= 65535:
            raise TapError("Profile port must be between 1024 and 65535")
        url = urlsplit(self.probe_url)
        if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password:
            raise TapError("Probe must be an HTTP(S) URL without credentials")
        if not Path(self.backend).is_absolute():
            raise TapError("Backend must be an explicit absolute path")
        if not isinstance(self.addons, list) or not all(isinstance(p, str) and Path(p).is_absolute() for p in self.addons):
            raise TapError("Additional addons must be explicit absolute paths")

    @property
    def label(self):
        return "com.tap.core." + hashlib.sha256(str(self.root).encode()).hexdigest()[:16]

    @property
    def plist(self):
        return Path.home() / "Library/LaunchAgents" / (self.label + ".plist")

    @property
    def snapshot(self):
        return self.root / "state/proxy-before.json"

    def save(self):
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root.chmod(0o700)
        for directory in ("data", "state", "certificates", "logs"):
            path = self.root / directory
            if path.is_symlink():
                raise TapError(f"Profile-owned directory must not be a symlink: {directory}")
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.chmod(0o700)
        data = asdict(self)
        del data["root"]
        atomic_json(self.root / "profile.json", data)

    @classmethod
    def load(cls, root):
        try:
            return cls(root=root, **json.loads((root / "profile.json").read_text()))
        except (OSError, ValueError, TypeError) as error:
            raise TapError(f"Cannot load profile: {error}") from error


@contextmanager
def profile_lock(root):
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (root / "command.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise TapError("Another command is changing this profile") from error
        yield lock


class MacOS:
    """All OS operations live here; lifecycle tests provide controlled adapters."""
    def run(self, args, *, check=True, timeout=15):
        try:
            result = subprocess.run([str(arg) for arg in args], text=True, capture_output=True,
                                    timeout=timeout, env={**os.environ, "LC_ALL": "C"})
        except (OSError, subprocess.TimeoutExpired) as error:
            raise TapError(f"Cannot run {args[0]}: {error}") from error
        if check and result.returncode:
            raise TapError(f"{args[0]} failed: {result.stderr.strip() or result.stdout.strip()}")
        return result

    def backend_version(self, profile):
        output = self.run([profile.backend, "--version"]).stdout
        if not re.search(r"^Mitmproxy: " + re.escape(BACKEND_VERSION) + r"(?:\s|$)", output, re.M):
            raise TapError(f"This checkout requires mitmproxy {BACKEND_VERSION}; got {output.strip()}")
        for path in [ADDON, *map(Path, profile.addons)]:
            if not path.is_file():
                raise TapError(f"Missing addon: {path}")
        return BACKEND_VERSION

    def target(self, profile):
        return f"gui/{os.getuid()}/{profile.label}"

    def service_info(self, profile):
        result = self.run(["/bin/launchctl", "print", self.target(profile)], check=False)
        if result.returncode == 0:
            return result.stdout
        if result.returncode == 113 and f'Could not find service "{profile.label}"' in result.stderr:
            return None
        raise TapError(f"Cannot inspect profile service: {result.stderr.strip() or result.stdout.strip() or result.returncode}")

    def service_pid(self, profile):
        info = self.service_info(profile)
        if info is None:
            return None
        match = re.search(r"^\s*pid = (\d+)\s*$", info, re.M)
        return int(match[1]) if match else None

    def service_loaded(self, profile):
        return self.service_info(profile) is not None

    def port_open(self, profile):
        try:
            with socket.create_connection(("127.0.0.1", profile.port), timeout=0.3):
                return True
        except ConnectionRefusedError:
            return False
        except OSError as error:
            raise TapError(f"Cannot inspect listener: {error}") from error

    def owns_port(self, profile):
        pid = self.service_pid(profile)
        if not pid:
            return False
        result = self.run(["/usr/sbin/lsof", "-nP", f"-iTCP:{profile.port}", "-sTCP:LISTEN", "-t"], check=False)
        if result.returncode == 1 and not result.stdout and not result.stderr:
            return False
        if result.stderr or result.returncode != 0:
            raise TapError(f"Cannot inspect listener ownership: {result.stderr.strip() or result.stdout.strip() or result.returncode}")
        owners = result.stdout.split()
        if not owners or not all(owner.isdigit() for owner in owners):
            raise TapError("Unrecognized listener ownership output")
        return set(owners) == {str(pid)}

    def write_plist(self, profile):
        args = [profile.backend, "--listen-host", "127.0.0.1", "-p", str(profile.port),
                "--set", "confdir=" + str(profile.root / "certificates"),
                "--set", "stream_large_bodies=4m", "-s", str(ADDON)]
        for addon in profile.addons:
            args.extend(["-s", addon])
        # Preserve the legacy shell ulimit wrapper; quote argv, never interpolate
        # raw paths into shell code. plistlib handles XML-sensitive characters.
        plist = {"Label": profile.label,
                 "ProgramArguments": ["/bin/bash", "-c", "ulimit -n 65536 || exit; exec " + shlex.join(args)],
                 "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 3,
                 "WorkingDirectory": str(profile.root),
                 "EnvironmentVariables": {"TAP_CORE_DATA": str(profile.root / "data"),
                                          "TAP_CORE_STATE": str(profile.root / "state")},
                 "StandardOutPath": str(profile.root / "logs/capture.log"),
                 "StandardErrorPath": str(profile.root / "logs/capture.log")}
        profile.plist.parent.mkdir(parents=True, exist_ok=True)
        temporary = profile.plist.with_suffix(".tmp")
        temporary.write_bytes(plistlib.dumps(plist))
        temporary.chmod(0o600)
        temporary.replace(profile.plist)

    def wait(self, predicate, seconds=15):
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            if predicate():
                return True
            time.sleep(0.25)
        return predicate()

    def start(self, profile):
        if self.owns_port(profile) and self.port_open(profile):
            return
        if self.port_open(profile):
            raise TapError(f"Port {profile.port} is occupied; its owner will not be stopped")
        if self.service_loaded(profile):
            self.stop(profile)
        try:
            self.write_plist(profile)
            self.run(["/bin/launchctl", "bootstrap", f"gui/{os.getuid()}", profile.plist])
            if not self.wait(lambda: self.owns_port(profile) and self.port_open(profile)):
                raise TapError(f"Profile service did not acquire port {profile.port}; see {profile.root / 'logs/capture.log'}")
        except (TapError, OSError) as error:
            # Do not stop here: an earlier session may still have armed routing.
            raise StartupError(str(error)) from error

    def stop(self, profile):
        # Remove autoload first, but still attempt bootout if removal fails.
        errors = []
        for path in (profile.plist, profile.plist.with_suffix(".tmp")):
            try:
                path.unlink(missing_ok=True)
            except OSError as error:
                errors.append(f"Cannot remove {path}: {error}")
        try:
            # Stop the exact job. Never pkill by port or an addon substring.
            pid = self.service_pid(profile)
            if self.service_loaded(profile):
                self.run(["/bin/launchctl", "bootout", self.target(profile)])
            def stopped():
                if self.service_loaded(profile):
                    return False
                if pid:
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        return True
                    return False
                return True
            if not self.wait(stopped):
                raise TapError("Profile service did not stop")
        except (TapError, OSError) as error:
            errors.append(str(error))
        if errors:
            raise TapError("Profile cleanup FAILED: " + "; ".join(errors))

    def flows(self, profile):
        for _ in range(3):
            result = self.run(["/usr/bin/curl", "--silent", "--show-error", "--fail", "--noproxy", "",
                               "--max-time", "5", "--proxy", f"http://127.0.0.1:{profile.port}",
                               "--output", "/dev/null", profile.probe_url], check=False, timeout=7)
            if result.returncode == 0:
                return True
        return False

    def services(self):
        lines = self.run([NS, "-listallnetworkservices"]).stdout.splitlines()
        services = [line for line in lines[1:] if line and not line.startswith("*")]
        if not services:
            raise TapError("No enabled network services could be enumerated")
        return services

    def proxy(self, service, secure=False):
        output = self.run([NS, "-getsecurewebproxy" if secure else "-getwebproxy", service]).stdout
        values = dict(line.split(": ", 1) for line in output.splitlines() if ": " in line)
        if values.get("Enabled") not in ("Yes", "No") or "Server" not in values or "Port" not in values:
            raise TapError(f"Cannot read proxy state for {service}")
        return {"enabled": values["Enabled"] == "Yes", "server": values["Server"], "port": int(values["Port"])}

    def bypass(self, service):
        lines = self.run([NS, "-getproxybypassdomains", service]).stdout.strip().splitlines()
        return [] if len(lines) == 1 and lines[0].startswith("There aren't any bypass domains") else lines

    def network_state(self):
        return {name: {"http": self.proxy(name), "https": self.proxy(name, True), "bypass": self.bypass(name)}
                for name in self.services()}

    def set_proxy(self, service, secure, state):
        kind = "securewebproxy" if secure else "webproxy"
        if not state["enabled"]:
            # Setting an endpoint can itself enable it. Disable directly instead
            # of briefly routing traffic through a previously configured server.
            self.run(["/usr/bin/sudo", "-n", NS, "-set" + kind + "state", service, "off"])
            return
        if state["server"] and state["port"]:
            self.run(["/usr/bin/sudo", "-n", NS, "-set" + kind, service, state["server"], str(state["port"])])
        self.run(["/usr/bin/sudo", "-n", NS, "-set" + kind + "state", service, "on" if state["enabled"] else "off"])

    def set_bypass(self, service, domains):
        self.run(["/usr/bin/sudo", "-n", NS, "-setproxybypassdomains", service, *(domains or ["Empty"])])

    def matches(self, actual, expected):
        # Disabled endpoints are retained to avoid briefly enabling them while
        # restoring a server address. This verifies routing, not every preference.
        return actual["enabled"] == expected["enabled"] and (
            not expected["enabled"] or (actual["server"], actual["port"]) == (expected["server"], expected["port"]))

    def armed(self, profile):
        expected = {"enabled": True, "server": "127.0.0.1", "port": profile.port}
        return all(self.matches(self.proxy(service, secure), expected)
                   for service in self.services() for secure in (False, True))

    def bypasses_match(self, before):
        if set(self.services()) != set(before):
            return False
        return all(self.bypass(service) == list(dict.fromkeys([*state["bypass"], "localhost", "127.0.0.1", "*.local"]))
                   for service, state in before.items())

    def arm(self, profile):
        if profile.snapshot.exists():
            before = json.loads(profile.snapshot.read_text())
            if self.armed(profile) and self.bypasses_match(before):
                return
            raise TapError("Saved network state needs recovery; run off before enabling again")
        before = self.network_state()
        if any(state[kind]["enabled"] for state in before.values() for kind in ("http", "https")):
            raise TapError("An existing system proxy is enabled; refusing to replace another installation")
        atomic_json(profile.snapshot, before)  # persist before the first mutation
        expected = {"enabled": True, "server": "127.0.0.1", "port": profile.port}
        for service, state in before.items():
            self.set_proxy(service, False, expected)
            self.set_proxy(service, True, expected)
            self.set_bypass(service, list(dict.fromkeys([*state["bypass"], "localhost", "127.0.0.1", "*.local"])))
        if not self.armed(profile):
            raise TapError("Could not verify both proxies on every network service")
        if not self.bypasses_match(before):
            raise TapError("Could not verify bypass domains on every service")

    def disarm(self, profile):
        if not profile.snapshot.exists():
            # No ownership journal: never modify someone else's routing.
            for service in self.services():
                for secure in (False, True):
                    state = self.proxy(service, secure)
                    if state["enabled"] and state["server"] in ("127.0.0.1", "localhost") and state["port"] == profile.port:
                        raise TapError("Profile proxy is enabled but its recovery snapshot is missing; refusing to stop")
            return
        before = json.loads(profile.snapshot.read_text())
        errors = []
        owned = {"enabled": True, "server": "127.0.0.1", "port": profile.port}
        # Do not overwrite an unrelated proxy enabled after we took the snapshot.
        for service in before:
            for secure in (False, True):
                current = self.proxy(service, secure)
                if current["enabled"] and not self.matches(current, owned):
                    raise TapError(f"Proxy changed outside this profile: {service}; recovery needs inspection")
        for service, state in before.items():
            for secure, kind in ((False, "http"), (True, "https")):
                try:
                    self.set_proxy(service, secure, state[kind])
                except TapError as error:
                    errors.append(str(error))
            try:
                self.set_bypass(service, state["bypass"])
            except TapError as error:
                errors.append(str(error))
        for service, state in before.items():
            if not all(self.matches(self.proxy(service, secure), state[kind]) for secure, kind in ((False, "http"), (True, "https"))):
                errors.append(f"Proxy restoration not verified: {service}")
            if self.bypass(service) != state["bypass"]:
                errors.append(f"Bypass restoration not verified: {service}")
        for service in self.services():
            if service not in before and any(self.matches(self.proxy(service, secure), owned) for secure in (False, True)):
                errors.append(f"New service points to this profile without a recovery snapshot: {service}")
        if errors:
            raise TapError("Network recovery incomplete; service kept running: " + "; ".join(errors))
        profile.snapshot.unlink()


class Lifecycle:
    def __init__(self, profile, os_adapter):
        self.profile, self.os = profile, os_adapter

    def recover(self, failure, cleanup=False):
        if self.profile.routing == "system":
            try:
                self.os.disarm(self.profile)
            except (TapError, OSError, ValueError) as error:
                raise TapError(f"{failure}; rollback FAILED: {error}. Capture was not stopped.") from error
            routing = "previous proxy routing restored"
        else:
            routing = "system proxy settings were not changed"
        if cleanup:
            try:
                self.os.stop(self.profile)
            except (TapError, OSError) as error:
                raise TapError(f"{failure}; {routing}; startup cleanup FAILED: {error}") from error
            routing += "; failed startup job and autoload removed"
        raise TapError(f"{failure}; {routing}")

    def install(self):
        self.os.backend_version(self.profile)
        if self.os.port_open(self.profile):
            raise TapError("Port is occupied; install will not replace its owner")
        self.profile.save()
        try:
            self.os.start(self.profile)
        except (TapError, OSError) as error:
            self.recover(f"Installation failed: {error}", cleanup=isinstance(error, StartupError))
        return "Installed profile service; use on to verify traffic"

    def on(self):
        try:
            self.os.backend_version(self.profile)
            self.os.start(self.profile)
        except (TapError, OSError) as error:
            # A saved snapshot means routing may already be armed after a crash.
            if self.profile.snapshot.exists() or isinstance(error, StartupError):
                self.recover(f"Capture startup failed: {error}", cleanup=isinstance(error, StartupError))
            raise TapError(f"Capture startup failed: {error}; proxy settings were not changed") from error
        try:
            if self.profile.routing == "system":
                self.os.arm(self.profile)
            if not self.os.flows(self.profile):
                raise TapError("No successful HTTP response through the profile proxy")
        except (TapError, OSError) as error:
            self.recover(str(error))
        if self.profile.routing == "explicit":
            return f"ON — explicit proxy 127.0.0.1:{self.profile.port}; system settings unchanged"
        return "ON — HTTP and HTTPS proxy settings verified on all enabled services; traffic probe passed"

    def off(self):
        if self.profile.routing == "system":
            self.os.disarm(self.profile)  # exception prevents stop
        self.os.stop(self.profile)
        if self.profile.routing == "explicit":
            return "OFF — profile service stopped; explicit clients must stop using its proxy endpoint"
        return "OFF — previous proxy routing restored, profile service stopped"
