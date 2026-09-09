"""Private, explicitly granted request-header observation; never journal records."""
import os
from pathlib import Path
import queue
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tap_core.runtime import profile_lock, TapError
from tap_core.pack_store import PackStore
from tap_core.commands import discover, _private_runtime_directory

HEADERS = ('cookie', 'authorization', 'user-agent')


def consume(root, origin, path, headers):
    # Recheck current selection/grants while holding the same mutation lease.
    # Queued observations cannot resurrect a disabled pack's access.
    root = Path(root).resolve()
    with profile_lock(root):
        store = PackStore(root)
        registry = store.load()
        available = {c.provider_id for c in discover(root).commands.values() if c.source != 'builtin'}
        for pack_id, record in registry['packs'].items():
            if not record['enabled'] or pack_id not in available:
                continue
            _, manifest = store.verify(registry, pack_id, record['selected'])
            policy = manifest['entrypoints'].get('command', {}).get('session_headers')
            if not policy or origin not in record['grants']['origins'] or not path.startswith(policy['path_prefix']):
                continue
            if 'session.observe' not in record['grants']['capabilities']:
                continue
            directory = _private_runtime_directory(store, Path(root) / 'state/packs' / pack_id / 'auth')
            host = urlsplit(origin).hostname
            for name in policy['headers']:
                value = headers.get(name)
                if not value:
                    continue
                destination = directory / (host + '.' + name)
                if destination.is_symlink():
                    raise TapError('Unsafe session observation destination')
                if destination.exists() and destination.read_text() == value:
                    continue
                fd, temporary = tempfile.mkstemp(dir=directory)
                try:
                    with os.fdopen(fd, 'w') as stream:
                        stream.write(value)
                    os.replace(temporary, destination)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)


class SessionObservation:
    def __init__(self):
        self.pending = queue.Queue(maxsize=16)
        self.stopped = threading.Event()
        self.worker = None
        self.routes = ()

    def load(self, loader):
        self.root = Path(os.environ['TAP_CORE_PROFILE'])
        self.worker = threading.Thread(target=self.drain, daemon=True)
        self.worker.start()

    def requestheaders(self, flow):
        request = flow.request
        if request.scheme != 'https':
            return
        origin = 'https://' + request.host.lower()
        if request.port != 443:
            return
        path = request.path.split('?', 1)[0]
        if not any(origin == allowed and path.startswith(prefix) for allowed, prefix in self.routes):
            return
        headers = {name: request.headers.get(name, '') for name in HEADERS}
        # At most 1 MiB queued, with no disk/discovery work on the proxy hook.
        if sum(len(value.encode()) for value in headers.values()) > 65536:
            return
        try:
            self.pending.put_nowait((origin, request.path.split('?', 1)[0], headers))
        except queue.Full:
            pass  # Passive best effort; subsequent browser traffic refreshes it.

    def drain(self):
        refreshed = 0
        while not self.stopped.is_set():
            if time.monotonic() - refreshed >= 1:
                try:
                    store = PackStore(self.root)
                    registry = store.load()
                    routes = []
                    for pack_id, record in registry['packs'].items():
                        if record['enabled'] and 'session.observe' in record['grants']['capabilities']:
                            _, manifest = store.verify(registry, pack_id, record['selected'])
                            policy = manifest['entrypoints'].get('command', {}).get('session_headers')
                            if policy:
                                routes.extend((origin, policy['path_prefix']) for origin in record['grants']['origins'])
                    self.routes = tuple(routes)
                except (TapError, OSError, ValueError):
                    self.routes = ()
                refreshed = time.monotonic()
            try:
                item = self.pending.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                consume(self.root, *item)
            except (TapError, OSError, ValueError):
                pass  # Never log header values; busy profiles retry on later traffic.
            finally:
                self.pending.task_done()

    def done(self):
        self.stopped.set()
        if self.worker:
            self.worker.join(timeout=1)


addons = [SessionObservation()]
