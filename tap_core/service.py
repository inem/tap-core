"""Owned Hub + finite reader scheduling; no application workflow semantics."""
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tap_core.runtime import Profile, atomic_json, profile_lock, stamped
from tap_core.components import ROOT, identity, hub_health, needs_hub
from tap_core.readers import Reader
from tap_core.fs_watch import GrowthSignal, WatchUnavailable
from tap_core import routes

# Readers sleep until the capture stream grows (kqueue on the file and its
# rotation), with a long backstop. Scans keep the previous pacing as a floor,
# so a busy stream costs no more than the former fixed 0.25 s loop.
READER_MIN_SCAN_SECONDS = 0.25
# A caught-up reader rescans no sooner than this. The capture stream is rarely
# quiet, so growth alone would rescan every few hundred milliseconds; scans
# that deliver nothing are what the controller's CPU goes to (measured live).
READER_IDLE_SCAN_SECONDS = 1.5
READER_IDLE_BACKSTOP_SECONDS = 30.0
# Records advanced per replay run. Filtered records are cheap (no child process,
# no per-record checkpoint write); each admitted record is still one invocation.
REPLAY_BATCH = 500
# Routed readers are woken by capture only for records that match their filter
# (tap_core/routes.py). The backstop bounds the delay after a lost notification.
ROUTED_BACKSTOP_SECONDS = 180.0
# Capture re-reads routes at most once a second, so a record written just after
# a reader attaches may go unannounced: one extra pass shortly after attaching.
ROUTE_SETTLE_SECONDS = 2.5


def service_startup_seconds():
    """Readiness grace for each managed service start (#247).

    Services are workloads, not infrastructure: the controller reports ready
    without waiting for them, and a service still closed after this grace is
    marked degraded and left to the bounded restart supervisor.
    TAP_SERVICE_STARTUP_SECONDS overrides the default; a bad value falls back.
    """
    try:
        return max(0.0, float(os.environ.get('TAP_SERVICE_STARTUP_SECONDS', 10)))
    except ValueError:
        return 10.0


def log(message):
    """One timestamped controller line into components.log (see #145)."""
    print(stamped(message), flush=True)


def main():
    os.umask(0o077)
    profile = Profile.load(Path(sys.argv[1]))
    stopping = threading.Event()
    refresh_requested = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    signal.signal(signal.SIGINT, lambda *_: stopping.set())
    # A pack mutation signals this controller, not the capture proxy or Hub.
    signal.signal(signal.SIGUSR1, lambda *_: refresh_requested.set())
    rows, services, reader_jobs = {}, {}, {}
    mutex = threading.Lock()
    health_path = profile.root / 'state/components.json'
    configuration = identity(profile)
    growth = GrowthSignal(profile.root / 'data/stream.jsonl')
    wake = {}            # reader name -> Event set by capture notifications
    attached_at = {}     # reader name -> monotonic attach time
    routing = {'listener': None}

    def on_targets(targets):
        for target in targets:
            if target.startswith('reader:'):
                event = wake.get(target[len('reader:'):])
                if event is not None:
                    event.set()

    def wait_for_work(name, seen, started, stop_reader, cancelled, floor=READER_IDLE_SCAN_SECONDS):
        """Routed: sleep until capture reports a matching record (or the
        backstop). Unrouted fallback: the stream-growth wait with its floor."""
        if routing['listener'] is not None:
            if not cancelled():
                settle = attached_at.get(name, 0) + ROUTE_SETTLE_SECONDS - time.monotonic()
                wake[name].wait(settle + 0.05 if settle > 0 else ROUTED_BACKSTOP_SECONDS)
        else:
            idle_wait(seen, started, stop_reader, cancelled, floor)

    def idle_wait(seen, started, stop_reader, cancelled, floor=READER_IDLE_SCAN_SECONDS):
        stop_reader.wait(max(0.0, started + floor - time.monotonic()))
        if growth.available and not cancelled():
            growth.wait(seen, READER_IDLE_BACKSTOP_SECONDS, cancelled)

    def report(phase, hub_pid=None, error=None):
        with mutex:
            workloads_healthy = all(row['healthy'] for row in [*rows.values(), *services.values()])
            state = {'pid': os.getpid(), 'updated_at': time.time(), 'configuration': configuration,
                     'phase': phase, 'healthy': phase == 'ready' and not error,
                     'hub_pid': hub_pid, 'error': error,
                     'workloads_healthy': workloads_healthy, 'readers': dict(rows),
                     'services': dict(services)}
        atomic_json(health_path, state)
    def reader_loop(name, spec, allowed_origins, path_prefixes, fresh_enabled, stop_reader):
        def cancelled():
            return stopping.is_set() or stop_reader.is_set()

        path_prefixes = dict(path_prefixes) if path_prefixes else None
        reader, failures, first = Reader(profile, name), 0, True
        if fresh_enabled:
            return fresh_reader_loop(name, spec, allowed_origins, path_prefixes, stop_reader, cancelled)
        while not cancelled():
            seen, started, busy, delivered = growth.generation, time.monotonic(), False, False
            wake[name].clear()  # a notification during this pass triggers the next one
            try:
                batch = 1 if first else REPLAY_BATCH
                result = reader.run(spec, max_records=batch, timeout=45,
                                    guard_parent=True, cancelled=cancelled,
                                    allowed_origins=allowed_origins, path_prefixes=path_prefixes)
                failures, first = 0, False
                busy = result['completed_this_run'] >= batch  # a full batch: backlog remains
                delivered = result['invoked_this_run'] > 0
                row = {'healthy': True, 'phase': 'waiting', 'progress': reader.load(), 'error': None}
            except Exception as error:
                failures += 1
                row = {'healthy': False, 'phase': 'failed' if failures >= 3 else 'backoff',
                       'error': type(error).__name__ + ': ' + str(error), 'failures': failures}
            with mutex:
                rows[name] = row
            if failures >= 3:
                return  # Explicit off/on resumes; no infinite failed replay loop.
            if failures:
                stop_reader.wait(min(4, 2 ** (failures - 1)))
            elif not busy:
                wait_for_work(name, seen, started, stop_reader, cancelled,
                              READER_MIN_SCAN_SECONDS if delivered else READER_IDLE_SCAN_SECONDS)

    def fresh_reader_loop(name, spec, allowed_origins, path_prefixes, stop_reader, cancelled):
        replay = Reader(profile, name)
        fresh = Reader(profile, name, lane='fresh')
        initialized = False
        replay_failures = fresh_failures = 0
        replay_error = fresh_error = None
        while not cancelled():
            seen, started, busy, delivered = growth.generation, time.monotonic(), False, False
            wake[name].clear()  # a notification during this pass triggers the next one
            if not initialized:
                try:
                    fresh.follow_tail(spec)
                    initialized = True
                    fresh_failures = 0
                except Exception as error:
                    fresh_failures += 1
                    fresh_error = type(error).__name__ + ': ' + str(error)
            if initialized:
                try:
                    result = fresh.run(spec, max_records=500, max_invocations=5, timeout=45, guard_parent=True,
                                       cancelled=cancelled, allowed_origins=allowed_origins,
                                       path_prefixes=path_prefixes)
                    busy = result['completed_this_run'] > 0
                    delivered = result['invoked_this_run'] > 0
                    fresh_failures = 0
                    if not any(fresh.state.glob('fresh-failure-*.json')):
                        fresh_error = None
                except Exception as error:
                    fresh_failures += 1
                    fresh_error = type(error).__name__ + ': ' + str(error)
                    if fresh_failures >= 3:
                        try:
                            fresh.skip_failed_fresh(spec)
                            fresh_failures = 0
                        except Exception as skip_error:
                            fresh_error += '; recovery failed: ' + str(skip_error)
            if replay_failures < 3:
                try:
                    # Recovery stays gentle (one invocation per pass) while
                    # filtered records no longer cost a pass each.
                    result = replay.run(spec, max_records=REPLAY_BATCH, max_invocations=1, timeout=45,
                                        guard_parent=True, cancelled=cancelled,
                                        allowed_origins=allowed_origins, path_prefixes=path_prefixes)
                    busy = busy or result['completed_this_run'] > 0
                    delivered = delivered or result['invoked_this_run'] > 0
                    replay_failures = 0
                    replay_error = None
                except Exception as error:
                    replay_failures += 1
                    replay_error = type(error).__name__ + ': ' + str(error)
            error = fresh_error or replay_error
            failures_count = fresh_failures or replay_failures
            row = {'healthy': error is None, 'phase': 'waiting' if error is None else
                   ('failed' if replay_failures >= 3 else 'backoff'),
                   'progress': replay.load(), 'fresh_progress': fresh.load(),
                   'fresh_skipped': len(list(fresh.state.glob('fresh-failure-*.json'))), 'error': error}
            if error is not None:
                row['failures'] = failures_count
            with mutex:
                rows[name] = row
            if fresh_failures or not initialized:
                stop_reader.wait(min(4, 2 ** (max(fresh_failures, 1) - 1)))
            elif delivered or 0 < replay_failures < 3:
                stop_reader.wait(max(0.0, started + READER_MIN_SCAN_SECONDS - time.monotonic()))
            else:
                # Filtered-only or empty passes: wait for a matching record.
                wait_for_work(name, seen, started, stop_reader, cancelled)

    def sync_readers(desired_components, reader_origins, fresh_readers, reader_prefixes, *, live_attach=False):
        """Change only reader workers; never restart the controller, Hub or proxy."""
        desired = {name: (spec, reader_origins.get(name), reader_prefixes.get(name), name in fresh_readers)
                   for name, spec in desired_components['readers'].items()}
        replaced = set()
        # Stop changed workers before starting replacements: no duplicate writes.
        for name, (spec, origins, prefixes, fresh, stop_reader, thread) in list(reader_jobs.items()):
            if desired.get(name) == (spec, origins, prefixes, fresh):
                continue
            if name in desired:
                replaced.add(name)
            stop_reader.set()
            wake[name].set()  # interrupt a routed wait
            thread.join(timeout=4)
            if thread.is_alive():
                raise RuntimeError(f'Reader {name} did not stop; replacement was not started')
            reader_jobs.pop(name)
            wake.pop(name, None)
            attached_at.pop(name, None)
            with mutex:
                rows.pop(name, None)
            log(f'reader {name} detached')
        # Events and routes exist before a reader starts: a record written after
        # its first pass begins is either seen by that pass or announced.
        for name in desired:
            wake.setdefault(name, threading.Event())
        if routing['listener'] is not None:
            # Capture notifies a reader only for records inside its own filter.
            routes.publish(profile.root / 'state', 'components', {
                'reader:' + name: {'origins': origins, 'path_prefixes': dict(prefixes) if prefixes else {}}
                for name, (_spec, origins, prefixes, _fresh) in desired.items()}, routing['listener'].path)
        for name, (spec, origins, prefixes, fresh) in desired.items():
            if name in reader_jobs:
                continue
            # Newly attached readers are subscriptions to future events only.
            # Startup readers and in-place live updates retain persisted progress.
            if live_attach and name not in replaced:
                Reader(profile, name).begin_now(spec)
                if fresh:
                    Reader(profile, name, lane='fresh').begin_now(spec)
            stop_reader = threading.Event()
            attached_at[name] = time.monotonic()
            thread = threading.Thread(target=reader_loop,
                                      args=(name, spec, origins, prefixes, fresh, stop_reader),
                                      daemon=True)
            with mutex:
                rows[name] = {'healthy': False, 'phase': 'starting', 'error': None}
            reader_jobs[name] = (spec, origins, prefixes, fresh, stop_reader, thread)
            thread.start()
            log(f'reader {name} attached')

    with profile_lock(profile.root / 'state/component-service', busy_message='Component controller already running'):
        starts_path = profile.root / 'state/component-starts.json'
        try:
            previous = json.loads(starts_path.read_text())['starts']
        except FileNotFoundError:
            previous = []
        recent = [t for t in previous if time.time() - 60 < t <= time.time()]
        if len(recent) >= 3:
            report('failed', error='Controller crash budget exhausted; inspect logs and use off/on')
            log('controller crash budget exhausted; inspect logs and use off/on')
            return 0  # launchd SuccessfulExit:false stops retrying.
        atomic_json(starts_path, {'starts': recent + [time.time()]})
        report('starting')
        from tap_core.pack_store import PackStore
        store = PackStore(profile.root)
        components = store.effective_components(profile.components)
        reader_origins = store.reader_origins()
        fresh_readers = store.fresh_readers()
        reader_prefixes = store.reader_path_prefixes()
        hub = None
        service_processes = {}
        hub_pid = None
        python_bin = sys.executable if components.get('python') == '/usr/bin/python3' else components.get('python', sys.executable)
        try:
            if needs_hub(components, profile.bridge):
                command = [python_bin, '-B', str(ROOT / 'guardian.py'), str(os.getpid()),
                           components['bun'], str(ROOT / 'hub.mjs'), str(profile.root)]
                hub = subprocess.Popen(command, start_new_session=True,
                                       env={**os.environ, 'TAP_PYTHON': python_bin})
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
                log(f'hub ready pid={hub_pid}')
            # Managed services start in parallel and are probed by the steady
            # loop below. Waiting for each here serialized their readiness in
            # front of the controller's own and could exhaust the lifecycle's
            # 15 s window, aborting the whole profile start (#247).
            startup_grace = service_startup_seconds()
            service_deadline = {}
            service_announced = {}
            for name, spec in components.get('services', {}).items():
                command = [python_bin, '-B', str(ROOT / 'guardian.py'), str(os.getpid()), *spec['command']]
                process = subprocess.Popen(command, start_new_session=True)
                service_processes[name] = (process, spec['port'])
                service_deadline[name] = time.monotonic() + startup_grace
                service_announced[name] = False
                services[name] = {'healthy': False, 'phase': 'starting', 'port': spec['port'], 'error': None}
                log(f'service {name} starting pid={process.pid} port={spec["port"]}')
            try:
                routing['listener'] = routes.WakeListener(
                    routes.socket_path(profile.root / 'state', 'components'), on_targets).start()
            except OSError as error:
                log(f'capture routing unavailable ({error}); readers wait on stream growth')
                try:
                    growth.start()
                except WatchUnavailable as error:
                    log(f'stream growth watch unavailable ({error}); readers poll every '
                        f'{READER_MIN_SCAN_SECONDS:g}s')
            sync_readers(components, reader_origins, fresh_readers, reader_prefixes)
            hub_failures = 0
            refresh_retry_at = 0.0
            refresh_failures = 0
            refresh_error = None
            service_failures = {name: 0 for name in service_processes}
            service_starts = {name: [time.monotonic()] for name in service_processes}

            def within_restart_budget(name):
                return len([t for t in service_starts[name] if time.monotonic() - 120 < t]) < 5

            def restart_service(name):
                log(f'service {name} restarting')
                old, port = service_processes[name]
                try:
                    os.killpg(old.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                spec = components['services'][name]
                command = [python_bin, '-B', str(ROOT / 'guardian.py'),
                           str(os.getpid()), *spec['command']]
                service_processes[name] = (subprocess.Popen(command, start_new_session=True), port)
                service_starts[name] = ([t for t in service_starts[name]
                                         if time.monotonic() - 120 < t] + [time.monotonic()])
                service_deadline[name] = time.monotonic() + startup_grace
                service_announced[name] = False

            log(f'controller ready hub_pid={hub_pid} services={sorted(service_processes)} '
                f'readers={sorted(components["readers"])}')
            while not stopping.is_set():
                if refresh_requested.is_set() and time.monotonic() >= refresh_retry_at:
                    # Consume the signal only for this attempt. A failure re-queues
                    # the request; another signal arriving during reconciliation
                    # remains pending rather than being cleared on success.
                    refresh_requested.clear()
                    try:
                        from tap_core.components import configuration as validate_components, prepare
                        updated = store.effective_components(profile.components)
                        validate_components(updated, profile)
                        # Handler/service/host changes still need a controlled stopped-profile update.
                        unchanged = lambda c: {k: v for k, v in c.items() if k != 'readers'}
                        if unchanged(updated) != unchanged(components):
                            raise RuntimeError('Live refresh supports reader changes only')
                        sync_readers(updated, store.reader_origins(), store.fresh_readers(),
                                     store.reader_path_prefixes(), live_attach=True)
                        components = updated
                        # The existing runtime snapshot and health identity must agree.
                        prepare(profile)
                        configuration = identity(profile)
                        refresh_failures = 0
                        refresh_retry_at = 0.0
                        refresh_error = None
                        log(f'live reader reconciliation: {sorted(reader_jobs)}')
                    except Exception as error:
                        # A saved registry change must remain actionable after a
                        # transient filesystem, lock or worker shutdown failure.
                        # Bounded backoff avoids a hot loop on permanent errors.
                        refresh_failures += 1
                        refresh_retry_at = time.monotonic() + min(30, 2 ** min(refresh_failures, 5))
                        refresh_error = f'Live reader reconciliation pending: {type(error).__name__}: {error}'
                        refresh_requested.set()
                        log(f'{refresh_error}; retrying')
                # The Hub is core infrastructure: only its own process exit tears
                # the controller down. Transient health-probe failures are
                # tolerated (5 in a row) so a busy machine or a heavy managed
                # service never kills page injection for every site.
                if hub is not None:
                    if hub.poll() is not None:
                        raise RuntimeError('Hub exited; inspect components.log and use off/on')
                    try:
                        if hub_health(profile)['pid'] != hub_pid:
                            raise RuntimeError('Hub identity changed')
                        hub_failures = 0
                    except OSError as error:
                        hub_failures += 1
                        if hub_failures >= 5:
                            raise RuntimeError('Hub unavailable or hung') from error
                # Managed services are isolated from the Hub and from each other:
                # a service that exits or stays unhealthy is restarted on its own
                # with a bounded budget, never tearing down the Hub or its peers.
                for name in list(service_processes):
                    process, port = service_processes[name]
                    if process.poll() is not None:
                        if within_restart_budget(name):
                            services[name] = {'healthy': False, 'phase': 'restarting', 'port': port, 'error': 'exited'}
                            restart_service(name)
                        else:
                            if services[name].get('phase') != 'failed':
                                log(f'service {name} restart budget exhausted; use off/on')
                            services[name] = {'healthy': False, 'phase': 'failed', 'port': port,
                                              'error': 'restart budget exhausted; use off/on'}
                        continue
                    try:
                        with socket.create_connection(('127.0.0.1', port), timeout=1.0):
                            service_failures[name] = 0
                            services[name] = {'healthy': True, 'phase': 'ready', 'port': port, 'error': None}
                            if not service_announced[name]:
                                service_announced[name] = True
                                log(f'service {name} ready pid={process.pid} port={port}')
                    except OSError as error:
                        if not service_announced[name] and time.monotonic() < service_deadline[name]:
                            # Still inside its startup grace: not a failure yet.
                            services[name] = {'healthy': False, 'phase': 'starting', 'port': port, 'error': None}
                            continue
                        if not service_announced[name]:
                            service_announced[name] = True
                            log(f'service {name} not ready within {startup_grace:g}s startup grace; '
                                f'degraded, supervisor retries pid={process.pid} port={port}')
                        service_failures[name] += 1
                        services[name] = {'healthy': False, 'phase': 'backoff', 'port': port, 'error': str(error)}
                        if service_failures[name] >= 5 and within_restart_budget(name):
                            service_failures[name] = 0
                            restart_service(name)
                report('ready', hub_pid, error=refresh_error)
                stopping.wait(0.5)
        except Exception as error:
            report('failed', error=str(error))
            log(str(error))
        finally:
            stopping.set()
            for *_job, stop_reader, _thread in reader_jobs.values():
                stop_reader.set()
            for event in wake.values():
                event.set()  # interrupt routed waits
            for *_job, thread in reader_jobs.values():
                thread.join(timeout=4)
            growth.close()
            if routing['listener'] is not None:
                routes.withdraw(profile.root / 'state', 'components')
                routing['listener'].close()
            if hub is not None:
                try:
                    os.killpg(hub.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                hub.wait(timeout=3)
            for process, _ in service_processes.values():
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass
        return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as error:
        # Invalid persisted configuration/state is not a reason for an infinite
        # launchd retry loop. SIGKILL/crash restarts use the budget in main().
        print(stamped(type(error).__name__ + ': ' + str(error)), flush=True)
        sys.exit(0)
