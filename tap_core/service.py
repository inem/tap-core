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


def log(message):
    """One timestamped controller line into components.log (see #145)."""
    print(stamped(message), flush=True)


def main():
    os.umask(0o077)
    profile = Profile.load(Path(sys.argv[1]))
    stopping = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    signal.signal(signal.SIGINT, lambda *_: stopping.set())
    rows, services, threads = {}, {}, []
    mutex = threading.Lock()
    health_path = profile.root / 'state/components.json'
    configuration = identity(profile)
    def report(phase, hub_pid=None, error=None):
        with mutex:
            workloads_healthy = all(row['healthy'] for row in [*rows.values(), *services.values()])
            state = {'pid': os.getpid(), 'updated_at': time.time(), 'configuration': configuration,
                     'phase': phase, 'healthy': phase == 'ready' and not error,
                     'hub_pid': hub_pid, 'error': error,
                     'workloads_healthy': workloads_healthy, 'readers': dict(rows),
                     'services': dict(services)}
        atomic_json(health_path, state)
    def reader_loop(name, spec, allowed_origins, fresh_enabled):
        reader, failures, first = Reader(profile, name), 0, True
        if fresh_enabled:
            return fresh_reader_loop(name, spec, allowed_origins)
        while not stopping.is_set():
            try:
                reader.run(spec, max_records=1 if first else 50, timeout=10, guard_parent=True,
                           cancelled=stopping.is_set, allowed_origins=allowed_origins)
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

    def fresh_reader_loop(name, spec, allowed_origins):
        replay = Reader(profile, name)
        fresh = Reader(profile, name, lane='fresh')
        initialized = False
        replay_failures = fresh_failures = 0
        replay_error = fresh_error = None
        while not stopping.is_set():
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
                    fresh.run(spec, max_records=500, max_invocations=5, timeout=10, guard_parent=True,
                              cancelled=stopping.is_set, allowed_origins=allowed_origins)
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
                    replay.run(spec, max_records=1, timeout=10,
                               guard_parent=True, cancelled=stopping.is_set,
                               allowed_origins=allowed_origins)
                    replay_failures = 0
                    replay_error = None
                except Exception as error:
                    replay_failures += 1
                    replay_error = type(error).__name__ + ': ' + str(error)
            error = fresh_error or replay_error
            row = {'healthy': error is None, 'phase': 'waiting' if error is None else
                   ('failed' if replay_failures >= 3 else 'backoff'),
                   'progress': replay.load(), 'fresh_progress': fresh.load(),
                   'fresh_skipped': len(list(fresh.state.glob('fresh-failure-*.json'))), 'error': error}
            with mutex:
                rows[name] = row
            stopping.wait(0.25 if fresh_failures == 0 else min(4, 2 ** (fresh_failures - 1)))
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
        hub = None
        service_processes = {}
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
                log(f'hub ready pid={hub_pid}')
            for name, spec in components.get('services', {}).items():
                command = [components['python'], '-B', str(ROOT / 'guardian.py'), str(os.getpid()), *spec['command']]
                process = subprocess.Popen(command, start_new_session=True)
                service_processes[name] = (process, spec['port'])
                services[name] = {'healthy': False, 'phase': 'starting', 'port': spec['port'], 'error': None}
            for name, (process, port) in service_processes.items():
                deadline = time.monotonic() + 10
                while not stopping.is_set() and process.poll() is None:
                    try:
                        with socket.create_connection(('127.0.0.1', port), timeout=1.0):
                            services[name] = {'healthy': True, 'phase': 'ready', 'port': port, 'error': None}
                            break
                    except OSError:
                        if time.monotonic() >= deadline:
                            break
                        stopping.wait(0.1)
                if not services[name]['healthy']:
                    # A managed service that is not ready at startup is left
                    # unhealthy for the steady-state loop to restart; a service
                    # must never tear down the Hub or its peers.
                    services[name] = {'healthy': False, 'phase': 'backoff', 'port': port,
                                      'error': 'not ready at startup'}
                log(f'service {name} {"ready" if services[name]["healthy"] else "not ready at startup"} '
                    f'pid={process.pid} port={port}')
            for name, spec in components['readers'].items():
                rows[name] = {'healthy': False, 'phase': 'starting', 'error': None}
                thread = threading.Thread(target=reader_loop,
                                          args=(name, spec, reader_origins.get(name), name in fresh_readers),
                                          daemon=True)
                threads.append(thread)
                thread.start()
            hub_failures = 0
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
                command = [components['python'], '-B', str(ROOT / 'guardian.py'),
                           str(os.getpid()), *spec['command']]
                service_processes[name] = (subprocess.Popen(command, start_new_session=True), port)
                service_starts[name] = ([t for t in service_starts[name]
                                         if time.monotonic() - 120 < t] + [time.monotonic()])

            log(f'controller ready hub_pid={hub_pid} services={sorted(service_processes)} '
                f'readers={sorted(components["readers"])}')
            while not stopping.is_set():
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
                    except OSError as error:
                        service_failures[name] += 1
                        services[name] = {'healthy': False, 'phase': 'backoff', 'port': port, 'error': str(error)}
                        if service_failures[name] >= 5 and within_restart_budget(name):
                            service_failures[name] = 0
                            restart_service(name)
                report('ready', hub_pid)
                stopping.wait(0.5)
        except Exception as error:
            report('failed', error=str(error))
            log(str(error))
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
