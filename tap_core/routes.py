"""Capture-side routing of record notifications to the consumers that want them.

Stdlib-only: the capture addon loads this file by path inside mitmproxy.

A consumer (the component controller for readers, the background host for
command triggers) publishes ``<state>/routes/<owner>.json``:

    {"version": 1, "socket": "<abs path>", "targets": {
        "reader:usage.meters": {"origins": ["https://claude.ai"],
                                "path_prefixes": {"https://claude.ai": ["/api/"]}}}}

and listens on that Unix datagram socket. After the capture writer has appended
a record to stream.jsonl it sends each owner whose targets match one datagram
``{"v": 1, "targets": [...]}``. A notification is only a hint: delivery is
best-effort and never blocks or fails capture. Consumers still read records
from their own cursor, so a missed datagram delays work until the next wake or
the consumer's backstop, and never loses a record.
"""
import hashlib
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
from urllib.parse import urlsplit

VERSION = 1
ALL_ORIGINS = "*"
MAX_DATAGRAM = 8192
_SUN_PATH_MAX = 100  # sockaddr_un.sun_path is 104 bytes on macOS, keep slack


def record_origin(record):
    """Return a captured URL's canonical HTTP origin, without site-specific routing."""
    url = record.get('url')
    if not isinstance(url, str):
        return None
    try:
        parts = urlsplit(url)
        if (parts.scheme not in ('http', 'https') or not parts.hostname
                or parts.username is not None or parts.password is not None):
            return None
        port = parts.port
    except ValueError:
        return None
    default = {'http': 80, 'https': 443}[parts.scheme]
    return f'{parts.scheme}://{parts.hostname}' + (f':{port}' if port is not None and port != default else '')


def record_matches(record, origins=None, path_prefixes=None, origin=None):
    """Whether a record falls inside an origin set and optional per-origin path prefixes.

    ``origins`` None means every origin; a set containing '*' too. An origin
    absent from ``path_prefixes`` is taken whole.
    """
    if origin is None and (origins is not None or path_prefixes):
        origin = record_origin(record)
    if origins is not None and ALL_ORIGINS not in origins and origin not in origins:
        return False
    prefixes = path_prefixes.get(origin) if path_prefixes else None
    if prefixes is None:
        return True
    try:
        return urlsplit(record['url']).path.startswith(tuple(prefixes))
    except (ValueError, KeyError, TypeError):
        return False


def routes_dir(state):
    return Path(state) / 'routes'


def socket_path(state, owner):
    """A bindable socket path: under the profile state when it fits sun_path,
    else in a private per-profile directory under the system temp dir."""
    preferred = Path(state) / 'wake' / (owner + '.sock')
    if len(os.fsencode(str(preferred))) <= _SUN_PATH_MAX:
        preferred.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        return preferred
    key = hashlib.sha256(str(Path(state).resolve()).encode()).hexdigest()[:16]
    directory = Path(tempfile.gettempdir()) / ('tap-wake-' + key)
    directory.mkdir(mode=0o700, exist_ok=True)
    return directory / (owner + '.sock')


def publish(state, owner, targets, sock):
    """Atomically (re)write this owner's routes. ``targets`` maps a target id to
    {'origins': iterable or None, 'path_prefixes': {origin: [prefix, ...]}}."""
    directory = routes_dir(state)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    value = {'version': VERSION, 'socket': str(sock), 'targets': {
        name: {'origins': None if spec.get('origins') is None else sorted(spec['origins']),
               'path_prefixes': {origin: list(prefixes) for origin, prefixes
                                 in dict(spec.get('path_prefixes') or {}).items()}}
        for name, spec in sorted(targets.items())}}
    path = directory / (owner + '.json')
    with tempfile.NamedTemporaryFile('w', dir=directory, delete=False, encoding='utf-8') as handle:
        json.dump(value, handle, sort_keys=True)
        temporary = Path(handle.name)
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    return path


def withdraw(state, owner):
    (routes_dir(state) / (owner + '.json')).unlink(missing_ok=True)


class WakeListener:
    """Receive notifications for one owner and hand target ids to ``on_targets``."""

    def __init__(self, path, on_targets):
        self.path = Path(path)
        self.on_targets = on_targets
        self.received = 0
        self._sock = None
        self._thread = None
        self._stop = threading.Event()

    def start(self):
        self.path.unlink(missing_ok=True)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            sock.bind(str(self.path))
            os.chmod(self.path, 0o600)
        except OSError:
            sock.close()
            raise
        sock.settimeout(1.0)
        self._sock = sock
        self._thread = threading.Thread(target=self._run, name='wake-listener', daemon=True)
        self._thread.start()
        return self

    def _run(self):
        while not self._stop.is_set():
            try:
                data = self._sock.recv(MAX_DATAGRAM)
            except socket.timeout:
                continue
            except OSError:
                if self._stop.is_set():
                    return
                time.sleep(0.1)
                continue
            try:
                message = json.loads(data)
                targets = message.get('targets') if message.get('v') == VERSION else None
            except (ValueError, AttributeError):
                continue
            if isinstance(targets, list):
                self.received += 1
                try:
                    self.on_targets([t for t in targets if isinstance(t, str)])
                except Exception as error:  # a consumer bug must not stop the listener
                    print(f'wake listener callback failed: {type(error).__name__}: {error}', flush=True)

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
        if self._sock is not None:
            self._sock.close()
        self.path.unlink(missing_ok=True)


class RouteTable:
    """Capture side: match a written record against published routes and notify.

    Route files are re-read at most every ``reload_seconds`` and only when the
    directory listing or a file's (mtime, size) changed.
    """

    def __init__(self, state, reload_seconds=1.0):
        self.directory = routes_dir(state)
        self.reload_seconds = reload_seconds
        self._checked = None  # monotonic time can start near 0: never assume a past check
        self._signature = None
        self._owners = []  # (socket path, [(target, origins set or None, prefixes)])
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self._sock.setblocking(False)
        self.sent = self.failed = 0

    def _signature_now(self):
        try:
            return tuple(sorted((entry.name, entry.stat().st_mtime_ns, entry.stat().st_size)
                                for entry in os.scandir(self.directory) if entry.name.endswith('.json')))
        except OSError:
            return ()

    def _reload(self):
        now = time.monotonic()
        if self._checked is not None and now - self._checked < self.reload_seconds:
            return
        self._checked = now
        signature = self._signature_now()
        if signature == self._signature:
            return
        owners = []
        for name, _, _ in signature:
            try:
                value = json.loads((self.directory / name).read_text(encoding='utf-8'))
                if value.get('version') != VERSION:
                    continue
                targets = [(target, None if spec.get('origins') is None else frozenset(spec['origins']),
                            {o: tuple(p) for o, p in (spec.get('path_prefixes') or {}).items()})
                           for target, spec in value['targets'].items()]
                owners.append((value['socket'], targets))
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                continue  # a half-written or foreign file is skipped until it changes
        self._owners, self._signature = owners, signature

    def notify(self, record):
        """Called after ``record`` is in stream.jsonl. Never raises."""
        try:
            self._reload()
            if not self._owners:
                return 0
            origin = record_origin(record)
            sent = 0
            for path, targets in self._owners:
                matched = [target for target, origins, prefixes in targets
                           if record_matches(record, origins, prefixes, origin=origin)]
                if not matched:
                    continue
                try:
                    self._sock.sendto(json.dumps({'v': VERSION, 'targets': matched}).encode(), path)
                    sent += 1
                except OSError:
                    self.failed += 1  # consumer down or busy: it catches up from its cursor
            self.sent += sent
            return sent
        except Exception:
            self.failed += 1
            return 0

    def close(self):
        self._sock.close()
