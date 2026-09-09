"""Profile-owned periodic command execution, independent of capture on/off."""
import hashlib
import json
import os
from pathlib import Path
import plistlib
import signal
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tap_core.runtime import TapError, atomic_json, command_execution_lock, profile_lock
from tap_core.pack_store import PackStore
from tap_core.commands import discover, _run_pack


def paths(root):
    key = hashlib.sha256(str(Path(root).resolve()).encode()).hexdigest()[:16]
    label = 'com.tap.core.background.' + key
    return label, Path.home() / 'Library/LaunchAgents' / (label + '.plist')


def tasks(root):
    store = PackStore(root)
    registry = store.load()
    commands = discover(root).commands
    result = []
    for pack_id, record in sorted(registry['packs'].items()):
        if not record['enabled']:
            continue
        _, manifest = store.verify(registry, pack_id, record['selected'])
        for declaration in manifest['entrypoints'].get('command', {}).get('commands', []):
            schedule = declaration.get('schedule')
            command = commands.get(tuple(declaration['path']))
            if schedule and command and command.provider_id == pack_id:
                result.append((command, schedule))
    return result


def read_state(root):
    try:
        return json.loads((Path(root) / 'state/background.json').read_text())
    except FileNotFoundError:
        return {'version': 1, 'jobs': {}}


def task_key(command):
    return command.provider_id + ':' + ' '.join(command.path)


def task_fingerprint(command, schedule):
    return hashlib.sha256(json.dumps(
        [command.provider_version, command.config, schedule], sort_keys=True).encode()).hexdigest()


def run_once(root):
    root = Path(root).resolve()
    # Discovery and schedule selection are a short profile snapshot. Provider
    # execution has its own inherited lease, so capture lifecycle remains
    # available while a network-bound periodic command is running.
    with profile_lock(root):
        state = read_state(root)
        active = tasks(root)
        keys = {task_key(command) for command, _ in active}
        state['jobs'] = {k: v for k, v in state['jobs'].items() if k in keys}
        due = []
        for command, schedule in active:
            key = task_key(command)
            previous = state['jobs'].get(key, {})
            fingerprint = task_fingerprint(command, schedule)
            now = time.time()
            if previous.get('fingerprint') == fingerprint and previous.get('next_at', 0) > now:
                continue
            due.append((key, fingerprint))
        atomic_json(root / 'state/background.json', state)

    for key, expected_fingerprint in due:
        # A command lease prevents pack disable/update/uninstall from invalidating
        # files or authority while the provider runs. Re-read the profile under
        # its short lease after acquiring execution authority so a stale
        # scheduler snapshot can never resurrect a changed selection.
        with command_execution_lock(root) as lease:
            with profile_lock(root):
                current = {task_key(command): (command, schedule)
                           for command, schedule in tasks(root)}.get(key)
                if current is None:
                    continue
                command, schedule = current
                fingerprint = task_fingerprint(command, schedule)
                if fingerprint != expected_fingerprint:
                    continue
                state = read_state(root)
                previous = state['jobs'].get(key, {})
                now = time.time()
                if previous.get('fingerprint') == fingerprint and previous.get('next_at', 0) > now:
                    continue
                run_id = hashlib.sha256(f'{key}\0{fingerprint}\0{time.time_ns()}'.encode()).hexdigest()
                row = {'provider': command.provider_id, 'version': command.provider_version,
                       'fingerprint': fingerprint, 'run': run_id,
                       'started_at': now, 'phase': 'running',
                       'next_at': now + schedule['interval_seconds']}
                state['jobs'][key] = row
                atomic_json(root / 'state/background.json', state)

            logdir = root / 'logs/background'
            logdir.mkdir(parents=True, exist_ok=True, mode=0o700)
            logpath = logdir / (hashlib.sha256(key.encode()).hexdigest()[:16] + '.log')
            # Bound each job's log between runs; never log invocation context.
            if logpath.exists() and logpath.stat().st_size > 1024 * 1024:
                logpath.unlink()
            try:
                with logpath.open('ab') as log:
                    os.chmod(logpath, 0o600)
                    code = _run_pack(command, root, [], lease_fd=lease.fileno(),
                                     timeout=schedule['timeout_seconds'], log=log)
            except (TapError, OSError):
                code = 125
                row['error'] = 'command_start_failed'

            with profile_lock(root):
                state = read_state(root)
                current_row = state['jobs'].get(key)
                if current_row is None or current_row.get('run') != run_id:
                    raise TapError(f'Background run state changed during execution: {key}')
                row.update(finished_at=time.time(), exit_code=code,
                           phase='unknown' if code in (124, 130) else 'ok' if code == 0 else 'failed')
                row['next_at'] = time.time() + schedule['interval_seconds']
                state['jobs'][key] = row
                atomic_json(root / 'state/background.json', state)

    with profile_lock(root):
        state = read_state(root)
        state['checked_at'] = time.time()
        atomic_json(root / 'state/background.json', state)


def reconcile(root, adapter, remove=False):
    root = Path(root).resolve()
    label, plist = paths(root)
    target = 'gui/' + str(os.getuid()) + '/' + label
    wanted = [] if remove else tasks(root)
    if not wanted and not plist.exists():
        return
    if not wanted:
        result = adapter.run(['/bin/launchctl', 'print', target], check=False)
        if result.returncode == 0:
            adapter.run(['/bin/launchctl', 'bootout', target])
        plist.unlink(missing_ok=True)
        return
    log = root / 'logs/background-host.log'
    log.parent.mkdir(parents=True, exist_ok=True)
    value = {'Label': label, 'ProgramArguments': [sys.executable, '-B', str(Path(__file__).resolve()), str(root)],
             'WorkingDirectory': str(root), 'RunAtLoad': True, 'StartInterval': 10,
             'StandardOutPath': str(log), 'StandardErrorPath': str(log)}
    data = plistlib.dumps(value)
    if plist.exists() and plist.read_bytes() != data:
        result = adapter.run(['/bin/launchctl', 'print', target], check=False)
        if result.returncode == 0:
            adapter.run(['/bin/launchctl', 'bootout', target])
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_bytes(data)
    plist.chmod(0o600)
    if adapter.run(['/bin/launchctl', 'print', target], check=False).returncode != 0:
        adapter.run(['/bin/launchctl', 'bootstrap', 'gui/' + str(os.getuid()), str(plist)])


def status(root):
    label, plist = paths(root)
    return {'registered': plist.exists(), 'label': label, **read_state(root)}


if __name__ == '__main__':
    os.umask(0o077)
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        run_once(Path(sys.argv[1]))
    except TapError as exc:
        # Busy is expected: next tick retries discovery, never stale pack code.
        print(str(exc), file=sys.stderr)
        sys.exit(1)
