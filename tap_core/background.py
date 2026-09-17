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
from tap_core.runtime import TapError, atomic_json, command_execution_lock, profile_lock, stamped
from tap_core.pack_store import PackStore
from tap_core.packs import PackError
from tap_core.commands import discover, _run_pack


def paths(root):
    key = hashlib.sha256(str(Path(root).resolve()).encode()).hexdigest()[:16]
    label = 'com.tap.core.background.' + key
    return label, Path.home() / 'Library/LaunchAgents' / (label + '.plist')


VERIFY_QUARANTINE_THRESHOLD = 5


def tasks(root, *, account=None):
    """Schedulable (command, schedule) pairs for enabled, verifiable packs.

    A pack whose integrity verify fails is skipped, never crashing the whole
    scheduler (#137). When `account` is the caller's mutable background state,
    consecutive failures are counted there; after VERIFY_QUARANTINE_THRESHOLD the
    pack is quarantined so it is neither verified, retried nor log-spammed every
    tick until its selected version changes or it is disabled (#139). A read-only
    caller (`account is None`, e.g. reconcile) honours an existing quarantine and
    otherwise skips a failing pack silently, without counting or logging.
    """
    store = PackStore(root)
    registry = store.load()
    commands = discover(root).commands
    failures = account.setdefault('verify_failures', {}) if account is not None \
        else read_state(root).get('verify_failures', {})
    result = []
    for pack_id, record in sorted(registry['packs'].items()):
        if not record['enabled']:
            failures.pop(pack_id, None)  # a disabled pack carries no quarantine
            continue
        version = record['selected']
        prior = failures.get(pack_id)
        if prior and prior.get('quarantined') and prior.get('version') == version:
            continue  # already quarantined at this version: no verify, no log
        try:
            _, manifest = store.verify(registry, pack_id, version)
        except (PackError, OSError) as exc:
            # Integrity failure, or an OS error reading installed pack code, must
            # not crash the scheduler (#137/#139).
            if account is None:
                continue
            count = prior['count'] + 1 if prior and prior.get('version') == version else 1
            quarantined = count >= VERIFY_QUARANTINE_THRESHOLD
            failures[pack_id] = {'version': version, 'count': count, 'error': str(exc),
                                 'at': time.time(), 'quarantined': quarantined}
            if quarantined:
                print(stamped('background: quarantined %s@%s after %d verify failures; '
                              'reinstall or re-enable to clear: %s' % (pack_id, version, count, exc)),
                      file=sys.stderr, flush=True)
            else:
                print(stamped('background: skipping %s@%s (verify failure %d/%d): %s' % (
                    pack_id, version, count, VERIFY_QUARANTINE_THRESHOLD, exc)), file=sys.stderr, flush=True)
            continue
        if account is not None:
            failures.pop(pack_id, None)  # verified: clear any prior failure/quarantine
        for declaration in manifest['entrypoints'].get('command', {}).get('commands', []):
            schedule = declaration.get('schedule')
            command = commands.get(tuple(declaration['path']))
            if schedule and command and command.provider_id == pack_id:
                result.append((command, schedule))
    if account is not None:
        for pack_id in [p for p in failures if p not in registry['packs']]:
            del failures[pack_id]  # forget uninstalled packs
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
        active = tasks(root, account=state)
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
            due.append((key, fingerprint, command.provider_id))
        atomic_json(root / 'state/background.json', state)

    for key, expected_fingerprint, provider_id in due:
        # A command lease prevents pack disable/update/uninstall from invalidating
        # files or authority while the provider runs. The lease is scoped to this
        # provider: operations on unrelated packs proceed without waiting.
        # Re-read the profile under its short lease after acquiring execution
        # authority so a stale scheduler snapshot can never resurrect a changed selection.
        with command_execution_lock(root, key=provider_id,
                                    busy_message=f"A command from pack '{provider_id}' is still running") as lease:
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
            print(stamped('ran %s:%s exit=%s %s' % (
                command.provider_id, '/'.join(command.path), code, row['phase'])), flush=True)

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
    state = read_state(root)
    quarantined = sorted(pid for pid, rec in state.get('verify_failures', {}).items()
                         if rec.get('quarantined'))
    return {'registered': plist.exists(), 'label': label, 'quarantined': quarantined, **state}


if __name__ == '__main__':
    os.umask(0o077)
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        run_once(Path(sys.argv[1]))
    except TapError as exc:
        # Busy is expected: next tick retries discovery, never stale pack code.
        print(stamped(str(exc)), file=sys.stderr, flush=True)
        sys.exit(1)
