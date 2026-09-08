"""Owned Hub + finite reader scheduling; no application workflow semantics."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tap_core.runtime import Profile, atomic_json, profile_lock
from tap_core.components import ROOT, identity, hub_health, needs_hub
from tap_core.readers import Reader


def main():
    os.umask(0o077)
    profile = Profile.load(Path(sys.argv[1]))
    stopping = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    signal.signal(signal.SIGINT, lambda *_: stopping.set())
    rows, threads = {}, []
    mutex = threading.Lock()
    health_path = profile.root / 'state/components.json'
    configuration = identity(profile)
    def report(phase, hub_pid=None, error=None):
        with mutex:
            state = {'pid': os.getpid(), 'updated_at': time.time(), 'configuration': configuration,
                     'phase': phase, 'healthy': phase == 'ready' and not error and all(row['healthy'] for row in rows.values()),
                     'hub_pid': hub_pid, 'error': error, 'readers': dict(rows)}
        atomic_json(health_path, state)
    def reader_loop(name, spec):
        reader, failures, first = Reader(profile, name), 0, True
        while not stopping.is_set():
            try:
                reader.run(spec, max_records=1 if first else 50, timeout=10, guard_parent=True, cancelled=stopping.is_set)
                failures, first = 0, False
                row = {'healthy': True, 'phase': 'waiting', 'progress': reader.load(), 'error': None}
            except Exception as error:
                failures += 1
                row = {'healthy': False, 'phase': 'failed' if failures >= 3 else 'backoff',
                       'error': type(error).__name__ + ': ' + str(error), 'failures': failures}
            with mutex:
                rows[name] = row
            if failures >= 3:
                return  # Explicit off/on resumes; no infinite failed replay loop.
            stopping.wait(min(4, 2 ** (failures - 1)) if failures else 0.25)
    with profile_lock(profile.root / 'state/component-service', busy_message='Component controller already running'):
        starts_path = profile.root / 'state/component-starts.json'
        try:
            previous = json.loads(starts_path.read_text())['starts']
        except FileNotFoundError:
            previous = []
        recent = [t for t in previous if time.time() - 60 < t <= time.time()]
        if len(recent) >= 3:
            report('failed', error='Controller crash budget exhausted; inspect logs and use off/on')
            return 0  # launchd SuccessfulExit:false stops retrying.
        atomic_json(starts_path, {'starts': recent + [time.time()]})
        report('starting')
        from tap_core.pack_store import PackStore
        store = PackStore(profile.root)
        components = store.effective_components(profile.components)
        hub = None
        hub_pid = None
        try:
            if needs_hub(components, profile.bridge):
                command = [components['python'], '-B', str(ROOT / 'guardian.py'), str(os.getpid()),
                           components['bun'], str(ROOT / 'hub.mjs'), str(profile.root)]
                hub = subprocess.Popen(command, start_new_session=True)
                deadline = time.monotonic() + 10
                while not stopping.is_set() and hub.poll() is None:
                    try:
                        hub_pid = hub_health(profile)['pid']
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise RuntimeError('Hub startup timed out')
                        stopping.wait(0.1)
                if hub_pid is None:
                    raise RuntimeError('Hub exited before readiness')
            for name, spec in components['readers'].items():
                rows[name] = {'healthy': False, 'phase': 'starting', 'error': None}
                thread = threading.Thread(target=reader_loop, args=(name, spec), daemon=True)
                threads.append(thread)
                thread.start()
            while not stopping.is_set():
                if hub is not None:
                    if hub.poll() is not None:
                        raise RuntimeError('Hub exited; inspect components.log and use off/on')
                    try:
                        if hub_health(profile)['pid'] != hub_pid:
                            raise RuntimeError('Hub identity changed')
                    except OSError as error:
                        raise RuntimeError('Hub unavailable or hung') from error
                report('ready', hub_pid)
                stopping.wait(0.5)
        except Exception as error:
            report('failed', error=str(error))
            print(str(error), flush=True)
        finally:
            stopping.set()
            for thread in threads:
                thread.join(timeout=4)
            if hub is not None:
                try:
                    os.killpg(hub.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                hub.wait(timeout=3)
        return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as error:
        # Invalid persisted configuration/state is not a reason for an infinite
        # launchd retry loop. SIGKILL/crash restarts use the budget in main().
        print(type(error).__name__ + ': ' + str(error), flush=True)
        sys.exit(0)
