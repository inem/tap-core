"""Host change notification: recursive path watches and append-file growth.

Two OS boundaries, kept apart from scheduling decisions:

* ``PathWatch`` — recursive change notification for directories or files via
  macOS FSEvents (CoreServices through ctypes, no extra runtime). A path that
  does not exist yet is watched through its nearest existing ancestor and
  events are filtered back to the requested path.
* ``GrowthSignal`` — a generation counter bumped when an append-only file is
  written, replaced (rotation) or its directory changes, via kqueue.

Neither interprets what changed. A missing OS facility raises
``WatchUnavailable`` at start; callers fall back explicitly and say so.
"""
import ctypes
import os
from pathlib import Path
import select
import sys
import threading
import time


class WatchUnavailable(OSError):
    pass


def expand(path, profile_root):
    """Resolve a declared watch path: '~/…', absolute, or profile-relative."""
    if path == '~' or path.startswith('~/'):
        path = str(Path.home()) + path[1:]
    elif not path.startswith('/'):
        path = str(Path(profile_root) / path)
    return os.path.realpath(path)


def _existing_ancestor(path):
    current = path
    while not os.path.exists(current):
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return current


def _related(event, watched):
    """An event concerns a watched path when it is that path or below it.

    Changes to an ancestor alone (a sibling created next to a path that does
    not exist yet) are not reported; the path's own creation is.
    """
    return event == watched or event.startswith(watched + '/')


# FSEvents constants (CoreServices/FSEvents.h).
_SINCE_NOW = 0xFFFFFFFFFFFFFFFF
_FLAG_NO_DEFER = 0x02
_FLAG_FILE_EVENTS = 0x10
_UTF8 = 0x08000100
_CALLBACK = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
                             ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
_api = None


def _fsevents():
    global _api
    if _api is not None:
        return _api
    if sys.platform != 'darwin':
        raise WatchUnavailable('FSEvents is available on macOS only')
    try:
        cf = ctypes.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
        cs = ctypes.CDLL('/System/Library/Frameworks/CoreServices.framework/CoreServices')
        system = ctypes.CDLL('/usr/lib/libSystem.dylib')
    except OSError as error:
        raise WatchUnavailable(f'FSEvents frameworks unavailable: {error}') from error
    void = ctypes.c_void_p
    cf.CFStringCreateWithCString.restype = void
    cf.CFStringCreateWithCString.argtypes = [void, ctypes.c_char_p, ctypes.c_uint32]
    cf.CFArrayCreate.restype = void
    cf.CFArrayCreate.argtypes = [void, ctypes.POINTER(void), ctypes.c_long, void]
    cf.CFRelease.argtypes = [void]
    cs.FSEventStreamCreate.restype = void
    cs.FSEventStreamCreate.argtypes = [void, _CALLBACK, void, void, ctypes.c_uint64,
                                       ctypes.c_double, ctypes.c_uint32]
    cs.FSEventStreamSetDispatchQueue.argtypes = [void, void]
    cs.FSEventStreamStart.restype = ctypes.c_ubyte
    cs.FSEventStreamStart.argtypes = [void]
    for name in ('FSEventStreamStop', 'FSEventStreamInvalidate', 'FSEventStreamRelease'):
        getattr(cs, name).argtypes = [void]
    system.dispatch_queue_create.restype = void
    system.dispatch_queue_create.argtypes = [ctypes.c_char_p, void]
    system.dispatch_release.argtypes = [void]
    callbacks = ctypes.addressof(ctypes.c_byte.in_dll(cf, 'kCFTypeArrayCallBacks'))
    _api = (cf, cs, system, callbacks)
    return _api


class PathWatch:
    """Call ``on_change(paths)`` from a private thread when watched paths change.

    ``latency`` is FSEvents' own coalescing window in seconds; debouncing a
    burst into one action is the caller's decision.
    """

    def __init__(self, paths, on_change, latency=0.05):
        self.paths = tuple(sorted({os.path.realpath(p) for p in paths}))
        self.on_change = on_change
        self.latency = latency
        self._stream = self._queue = None
        self._callback = _CALLBACK(self._deliver)  # kept alive with the stream

    def _deliver(self, _stream, _info, count, event_paths, _flags, _ids):
        try:
            names = ctypes.cast(event_paths, ctypes.POINTER(ctypes.c_char_p))
            changed = []
            for index in range(count):
                event = os.fsdecode(names[index])
                if any(_related(event, watched) for watched in self.paths):
                    changed.append(event)
            if changed:
                self.on_change(changed)
        except Exception as error:  # never unwind into CoreServices
            print(f'fs_watch: change callback failed: {type(error).__name__}: {error}',
                  file=sys.stderr, flush=True)

    def start(self):
        cf, cs, system, callbacks = _fsevents()
        roots = sorted({_existing_ancestor(path) for path in self.paths})
        strings = [cf.CFStringCreateWithCString(None, os.fsencode(root), _UTF8) for root in roots]
        values = (ctypes.c_void_p * len(strings))(*strings)
        array = cf.CFArrayCreate(None, values, len(strings), callbacks)
        for value in strings:
            cf.CFRelease(value)
        try:
            stream = cs.FSEventStreamCreate(None, self._callback, None, array, _SINCE_NOW,
                                            self.latency, _FLAG_NO_DEFER | _FLAG_FILE_EVENTS)
        finally:
            cf.CFRelease(array)
        if not stream:
            raise WatchUnavailable('FSEventStreamCreate failed')
        queue = system.dispatch_queue_create(b'tap.fs-watch', None)
        cs.FSEventStreamSetDispatchQueue(stream, queue)
        if not cs.FSEventStreamStart(stream):
            cs.FSEventStreamInvalidate(stream)
            cs.FSEventStreamRelease(stream)
            system.dispatch_release(queue)
            raise WatchUnavailable('FSEventStreamStart failed')
        self._stream, self._queue = stream, queue
        return self

    def close(self):
        if self._stream is None:
            return
        _, cs, system, _ = _fsevents()
        cs.FSEventStreamStop(self._stream)
        cs.FSEventStreamInvalidate(self._stream)
        cs.FSEventStreamRelease(self._stream)
        system.dispatch_release(self._queue)
        self._stream = self._queue = None


_EVTONLY = getattr(os, 'O_EVTONLY', 0x8000)


class GrowthSignal:
    """Generation counter for one append-only file and its rotations.

    A kqueue thread watches the file (write/extend/rename/delete) and its
    directory (entries added, renamed or removed). Any of them bumps the
    generation; after a rename or delete the file is reopened by name.
    """

    def __init__(self, path):
        self.path = Path(path)
        self.generation = 0
        self.available = False
        self.error = None
        self._condition = threading.Condition()
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if not hasattr(select, 'kqueue'):
            self.error = 'kqueue is not available on this platform'
            raise WatchUnavailable(self.error)
        try:
            self._kq = select.kqueue()
            self._dir = os.open(self.path.parent, _EVTONLY)
        except OSError as error:
            self.error = str(error)
            raise WatchUnavailable(self.error) from error
        self._file = None
        self._register(self._dir, select.KQ_NOTE_WRITE | select.KQ_NOTE_DELETE | select.KQ_NOTE_RENAME)
        self._reopen()
        self.available = True
        self._thread = threading.Thread(target=self._run, name='growth-signal', daemon=True)
        self._thread.start()
        return self

    def _register(self, fd, notes, flags=None):
        flags = select.KQ_EV_ADD | select.KQ_EV_CLEAR if flags is None else flags
        self._kq.control([select.kevent(fd, filter=select.KQ_FILTER_VNODE, flags=flags, fflags=notes)], 0, 0)

    def _reopen(self):
        if self._file is not None:
            os.close(self._file)  # closing removes its kevent
            self._file = None
        try:
            self._file = os.open(self.path, _EVTONLY)
        except FileNotFoundError:
            return  # the directory event announces its creation
        self._register(self._file, select.KQ_NOTE_WRITE | select.KQ_NOTE_EXTEND
                       | select.KQ_NOTE_DELETE | select.KQ_NOTE_RENAME)

    def _run(self):
        try:
            while not self._stop.is_set():
                events = self._kq.control(None, 8, 1.0)
                if not events:
                    continue
                if any(event.ident == self._dir or event.fflags & (select.KQ_NOTE_DELETE | select.KQ_NOTE_RENAME)
                       for event in events):
                    self._reopen()
                self.bump()
        except OSError as error:
            self.error = str(error)
            self.available = False
            self.bump()  # waiters must not sleep on a dead watch

    def bump(self):
        with self._condition:
            self.generation += 1
            self._condition.notify_all()

    def wait(self, seen, timeout, cancelled=None):
        """Block until the generation differs from ``seen``, ``timeout`` passes
        or ``cancelled()`` is true. Without a live watch, only time passes."""
        deadline = time.monotonic() + timeout
        with self._condition:
            while self.generation == seen and not (cancelled and cancelled()):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(min(remaining, 0.5))
            return self.generation

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
        for fd in (getattr(self, '_file', None), getattr(self, '_dir', None)):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        if hasattr(self, '_kq'):
            self._kq.close()
        self.available = False
