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
from tap_core import freshness_policy


def paths(root):
    key = hashlib.sha256(str(Path(root).resolve()).encode()).hexdigest()[:16]
    label = 'com.tap.core.background.' + key
    return label, Path.home() / 'Library/LaunchAgents' / (label + '.plist')


VERIFY_QUARANTINE_THRESHOLD = 5
FRESHNESS_WAKE_SECONDS = 15 * 60

# Transitional admission while #228 defines a pack-declared adapter contract.
# Only this local, credential-free adapter is allowed to refresh. Every other
# scheduled command keeps its existing schedule unchanged.
FRESHNESS_ADAPTERS = {
    ("usage.meters", ("usage", "collect")): {
        "provider": "codex",
        "freshness": ("data", "readers", "usage.meters", "freshness.json"),
    },
}


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


def freshness_adapter(command):
    return FRESHNESS_ADAPTERS.get((command.provider_id, command.path))


def _local_utc_offset_seconds(now):
    local = time.localtime(now)
    offset = getattr(local, "tm_gmtoff", None)
    if type(offset) is int:
        return offset
    return int(time.mktime(local) - time.mktime(time.gmtime(now)))


def _freshness_at(root, adapter):
    path = Path(root).joinpath(*adapter["freshness"])
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("version") != 1:
        return None
    observed = payload.get("observed_at")
    value = observed.get(adapter["provider"]) if isinstance(observed, dict) else None
    return value if type(value) in (int, float) else None


def _append_freshness_receipts(root, rows):
    if not rows:
        return
    path = Path(root) / "logs/usage-freshness-decisions.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    path.chmod(0o600)


def _freshness_account(state):
    account = state.setdefault("usage_freshness", {"version": 1, "next_wake_at": 0, "targets": {}})
    if account.get("version") != 1 or not isinstance(account.get("targets"), dict):
        account.clear()
        account.update({"version": 1, "next_wake_at": 0, "targets": {}})
    return account


def run_freshness_once(root, commands):
    """Coarse, policy-driven wake for the one admitted live usage adapter.

    This is deliberately narrow: it converts the old 180-second Codex poll into
    an idle-tier refresh with persisted policy state.  Passive activity and more
    adapters arrive through #223/#228; no command is inferred as refreshable.
    """
    root = Path(root).resolve()
    admitted = [(command, freshness_adapter(command)) for command, _ in commands
                if freshness_adapter(command) is not None]
    if not admitted:
        return []
    now = time.time()
    with profile_lock(root):
        state = read_state(root)
        account = _freshness_account(state)
        if account.get("next_wake_at", 0) > now:
            return []
        names = {command.provider_id + ":" + command.label for command, _ in admitted}
        account["targets"] = {name: value for name, value in account["targets"].items()
                              if name in names}
        policy_states, metadata = [], []
        for command, adapter in admitted:
            name = command.provider_id + ":" + command.label
            target = account["targets"].get(name)
            if not isinstance(target, dict):
                target = freshness_policy.new_state(adapter["provider"], adapter=command.provider_id + ":" + command.label)
            seen = _freshness_at(root, adapter)
            target = freshness_policy.observed_quota(target, seen, "legacy-schedule", now)
            policy_states.append(target)
            metadata.append((name, command, adapter, seen))
        context = freshness_policy.context(now, utc_offset_seconds=_local_utc_offset_seconds(now), online=None)
        policy_states, receipts = freshness_policy.plan(policy_states, context)
        account["targets"] = {name: target for (name, _, _, _), target in zip(metadata, policy_states)}
        account["next_wake_at"] = now + FRESHNESS_WAKE_SECONDS
        atomic_json(root / "state/background.json", state)
        _append_freshness_receipts(root, receipts)

    for index, receipt in enumerate(receipts):
        if receipt["action"] != freshness_policy.REFRESH:
            continue
        name, expected, adapter, previous = metadata[index]
        with command_execution_lock(root, key=expected.provider_id,
                                    busy_message=f"A command from pack '{expected.provider_id}' is still running") as lease:
            with profile_lock(root):
                current = {command.provider_id + ":" + command.label: command for command, _ in tasks(root)}.get(name)
                if current is None or current.provider_version != expected.provider_version:
                    continue
                state = read_state(root)
                account = _freshness_account(state)
                target = account["targets"].get(name, policy_states[index])
                target = freshness_policy.started(target, receipt, time.time())
                account["targets"][name] = target
                atomic_json(root / "state/background.json", state)
            try:
                code = _run_pack(current, root, [], lease_fd=lease.fileno(), timeout=60)
            except (TapError, OSError):
                code = 125
            finished_at = time.time()
            observed = _freshness_at(root, adapter)
            success = (code == 0 and type(observed) in (int, float)
                       and (previous is None or observed > previous))
            outcome = "unchanged" if success else "error"
            with profile_lock(root):
                state = read_state(root)
                account = _freshness_account(state)
                target = account["targets"].get(name)
                if target is not None:
                    target = freshness_policy.finished(
                        target, outcome, finished_at,
                        quota_observed_at=observed if success else None)
                    account["targets"][name] = target
                    outcome = "unchanged" if target.get("last_error") is None else "error"
                    atomic_json(root / "state/background.json", state)
            _append_freshness_receipts(root, [{
                "receipt": "tap.usage-freshness-execution/v1", "decision_id": receipt["decision_id"],
                "at": finished_at, "target": receipt["target"], "exit_code": code,
                "outcome": outcome,
            }])
    return receipts


def run_once(root):
    root = Path(root).resolve()
    # Discovery and schedule selection are a short profile snapshot. Provider
    # execution has its own inherited lease, so capture lifecycle remains
    # available while a network-bound periodic command is running.
    with profile_lock(root):
        state = read_state(root)
        active = tasks(root, account=state)
        scheduled = [(command, schedule) for command, schedule in active if freshness_adapter(command) is None]
        active_by_key = {task_key(command): command for command, _ in active}
        state['jobs'] = {k: v for k, v in state['jobs'].items()
                         if k in active_by_key and freshness_adapter(active_by_key[k]) is None}
        due = []
        for command, schedule in scheduled:
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

    run_freshness_once(root, active)


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
