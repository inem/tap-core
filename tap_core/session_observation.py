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
from tap_core.commands import _private_runtime_directory

HEADERS = ('cookie', 'authorization', 'user-agent')


def _candidates(store, registry, origin, path, only_pack=None):
    candidates = []
    for pack_id, record in registry['packs'].items():
        if only_pack is not None and pack_id != only_pack:
            continue
        grants = record.get('grants') or {}
        if (not record['enabled'] or origin not in grants.get('origins', ())
                or 'session.observe' not in grants.get('capabilities', ())):
            continue
        _, manifest = store.verify(registry, pack_id, record['selected'])
        policy = manifest['entrypoints'].get('command', {}).get('session_headers')
        if not policy or not path.startswith(policy['path_prefix']):
            continue
        candidates.append((pack_id, record['selected'], policy['headers']))
    return candidates


def consume(root, origin, path, headers, only_pack=None):
    """Publish one observation after a short final authorization check.

    Integrity verification stays outside the mutation lock. The selected version
    and grants are rechecked under the lock before any credential is published.
    """
    root = Path(root).resolve()
    store = PackStore(root)
    registry = store.load()
    candidates = _candidates(store, registry, origin, path, only_pack)
    if not candidates:
        return

    with profile_lock(root):
        current = store.load()
        for pack_id, version, names in candidates:
            record = current['packs'].get(pack_id)
            grants = record.get('grants') if record else None
            if (not record or not record['enabled'] or record['selected'] != version
                    or not grants or origin not in grants['origins']
                    or 'session.observe' not in grants['capabilities']):
                continue
            directory = _private_runtime_directory(store, Path(root) / 'state/packs' / pack_id / 'auth')
            host = urlsplit(origin).hostname
            for name in names:
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
        self.latest = {}
        self.latest_lock = threading.Lock()
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
        targets = {pack_id for pack_id, allowed, prefix in self.routes
                   if origin == allowed and path.startswith(prefix)}
        if not targets:
            return
        headers = {name: request.headers.get(name, '') for name in HEADERS}
        # At most 64 KiB per coalesced observation, with no disk work on the hook.
        if sum(len(value.encode()) for value in headers.values()) > 65536:
            return
        for pack_id in targets:
            key = (pack_id, origin)
            with self.latest_lock:
                if key in self.latest:
                    self.latest[key] = (origin, path, headers, pack_id)
                    continue
                self.latest[key] = (origin, path, headers, pack_id)
                try:
                    self.pending.put_nowait(key)
                except queue.Full:
                    self.latest.pop(key, None)
                    # Passive best effort; subsequent browser traffic refreshes it.

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
                                routes.extend((pack_id, origin, policy['path_prefix'])
                                              for origin in record['grants']['origins'])
                    self.routes = tuple(routes)
                except (TapError, OSError, ValueError):
                    self.routes = ()
                refreshed = time.monotonic()
            try:
                key = self.pending.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                with self.latest_lock:
                    item = self.latest.pop(key, None)
                if item is not None:
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
