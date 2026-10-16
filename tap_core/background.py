"""Profile-owned periodic command execution, independent of capture on/off."""
from contextlib import ExitStack
import hashlib
import json
import math
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
from tap_core.commands import discover, _run_pack, _start_pack
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
#
# One command may serve several targets: `usage collect` asks the Codex
# app-server and the Antigravity CLI in the same run. Each target has its own
# key in the pack's freshness sidecar and, optionally, its own passive activity
# source. The policy decides per target; the command runs once when any of its
# targets is due, and every target it serves is settled from its own key.
FRESHNESS_ADAPTERS = {
    ("usage.meters", ("usage", "collect")): {
        "targets": (
            {"provider": "codex",
             "freshness": ("data", "readers", "usage.meters", "freshness.json")},
            {"provider": "antigravity",
             "freshness": ("data", "readers", "usage.meters", "freshness.json"),
             "activity": {"path": ("data", "readers", "usage.meters", "native-activity.jsonl"),
                          "client": "antigravity-hub"}},
        ),
    },
}
# The activity ledger is append-only and roughly chronological; the newest row
# of a client is found in its tail, not by parsing every row ever written.
ACTIVITY_TAIL_BYTES = 1 << 20


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


def _freshness_at(root, target):
    path = Path(root).joinpath(*target["freshness"])
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("version") != 1:
        return None
    observed = payload.get("observed_at")
    value = observed.get(target["provider"]) if isinstance(observed, dict) else None
    return value if type(value) in (int, float) else None


def _activity_at(root, target):
    """Newest passive activity time of the target's client, or None.

    Reads the tail of the pack's activity ledger (schema-2 `activity` rows,
    one per line). A row of another client, a line that does not parse, and a
    non-finite time are skipped; the policy applies its own believability test
    to what is returned. The first line of a tail is usually cut and dropped.
    """
    spec = target.get("activity")
    if not spec:
        return None
    path = Path(root).joinpath(*spec["path"])
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            start = max(0, fh.tell() - ACTIVITY_TAIL_BYTES)
            fh.seek(start)
            lines = fh.read().split(b"\n")
    except OSError:
        return None
    if start:
        lines = lines[1:]
    newest = None
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict) or row.get("client") != spec["client"] \
                or row.get("record", "activity") != "activity":
            continue
        value = row.get("observed_at")
        if type(value) in (int, float) and math.isfinite(value) and (newest is None or value > newest):
            newest = value
    return newest


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


def _adapter_id(command):
    return command.provider_id + ":" + command.label


def _target_key(command, target):
    return _adapter_id(command) + "/" + target["provider"]


def _restore_target(account, command, target):
    key = _target_key(command, target)
    state = account["targets"].get(key)
    if isinstance(state, dict):
        return state
    # Before the second target, the one target was keyed by the adapter id
    # alone. Adopt it so its attempts and backoff survive the upgrade.
    legacy = account["targets"].get(_adapter_id(command))
    if isinstance(legacy, dict) and legacy.get("provider") == target["provider"]:
        return legacy
    return freshness_policy.new_state(target["provider"], adapter=_adapter_id(command))


def run_freshness_once(root, commands):
    """Coarse, policy-driven wake for the admitted live usage adapter.

    This is deliberately narrow: it converts the old 180-second provider poll
    into per-target policy decisions with persisted state. A command with
    several targets runs once per wake at most; the targets it serves are
    settled from their own freshness keys afterwards. More adapters arrive
    through #228; no command is inferred as refreshable.
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
        policy_states, metadata = [], []
        for command, adapter in admitted:
            for spec in adapter["targets"]:
                target = _restore_target(account, command, spec)
                seen = _freshness_at(root, spec)
                target = freshness_policy.observed_quota(target, seen, "legacy-schedule", now)
                target = freshness_policy.observed_activity(target, _activity_at(root, spec), now)
                policy_states.append(target)
                metadata.append((_target_key(command, spec), command, spec, seen))
        context = freshness_policy.context(now, utc_offset_seconds=_local_utc_offset_seconds(now), online=None)
        policy_states, receipts = freshness_policy.plan(policy_states, context)
        account["targets"] = {key: target for (key, _, _, _), target in zip(metadata, policy_states)}
        account["next_wake_at"] = now + FRESHNESS_WAKE_SECONDS
        atomic_json(root / "state/background.json", state)
        _append_freshness_receipts(root, receipts)

    # One run per command. The approved targets of a command share it, and the
    # run also serves the command's other targets (a coalesced neighbour, or
    # one that was simply fresh): each is settled from its own key.
    approved_by_command = {}
    for index, receipt in enumerate(receipts):
        if receipt["action"] == freshness_policy.REFRESH:
            approved_by_command.setdefault(_adapter_id(metadata[index][1]), []).append(index)
    for name, approved in approved_by_command.items():
        expected = metadata[approved[0]][1]
        served = [index for index, (_, command, _, _) in enumerate(metadata) if _adapter_id(command) == name]
        with command_execution_lock(root, key=expected.provider_id, shared=True,
                                    busy_message=f"A command from pack '{expected.provider_id}' is still running") as lease:
            with profile_lock(root):
                current = {_adapter_id(command): command for command, _ in tasks(root)}.get(name)
                if current is None or current.provider_version != expected.provider_version:
                    continue
                state = read_state(root)
                account = _freshness_account(state)
                started_at = time.time()
                for index in approved:
                    key = metadata[index][0]
                    target = account["targets"].get(key, policy_states[index])
                    account["targets"][key] = freshness_policy.started(target, receipts[index], started_at)
                atomic_json(root / "state/background.json", state)
            try:
                code = _run_pack(current, root, [], lease_fd=lease.fileno(), timeout=60)
            except (TapError, OSError):
                code = 125
            finished_at = time.time()
            rows = []
            with profile_lock(root):
                state = read_state(root)
                account = _freshness_account(state)
                for index in served:
                    key, _, spec, previous = metadata[index]
                    target = account["targets"].get(key)
                    if target is None:
                        continue
                    observed = _freshness_at(root, spec)
                    moved = (code == 0 and type(observed) in (int, float)
                             and (previous is None or observed > previous))
                    if index in approved or moved:
                        # A look that moved the key is a successful look for that
                        # target whether or not it asked: the streak clears. A key
                        # that did not move fails only the target that asked.
                        target = freshness_policy.finished(
                            target, "unchanged" if moved else "error", finished_at,
                            quota_observed_at=observed if moved else None)
                        account["targets"][key] = target
                        outcome = "unchanged" if target.get("last_error") is None else "error"
                    else:
                        outcome = "error"
                    decision = receipts[index if index in approved else approved[0]]
                    rows.append({
                        "receipt": "tap.usage-freshness-execution/v1", "decision_id": decision["decision_id"],
                        "at": finished_at, "target": receipts[index]["target"],
                        "exit_code": code, "outcome": outcome, "ride_along": index not in approved,
                    })
                atomic_json(root / "state/background.json", state)
            _append_freshness_receipts(root, rows)
    return receipts


# One host, existing provider subprocesses, no additional service or queue store.
MAX_RUNNING_PACKS = 4
DISCOVERY_SECONDS = 10
POLL_SECONDS = 0.1


def _scheduled_snapshot(root, *, account_failures=False):
    with profile_lock(root):
        state = read_state(root)
        active = tasks(root, account=state if account_failures else None)
        scheduled = [(c, s) for c, s in active if freshness_adapter(c) is None]
        keys = {task_key(c) for c, _ in scheduled}
        state['jobs'] = {k: v for k, v in state['jobs'].items() if k in keys}
        due = []
        for command, schedule in scheduled:
            key = task_key(command)
            previous = state['jobs'].get(key, {})
            fingerprint = task_fingerprint(command, schedule)
            same = previous.get('fingerprint') == fingerprint
            next_at = previous.get('next_at', 0) if same else 0
            if next_at <= time.time():
                due.append((next_at, key, fingerprint, command.provider_id))
        state['checked_at'] = time.time()
        atomic_json(root / 'state/background.json', state)
    # Oldest due first: a fast recurring command cannot jump an overdue neighbour.
    return active, sorted(due, key=lambda item: item[0])


def _start_scheduled(root, key, expected_fingerprint, provider_id):
    resources = ExitStack()
    try:
        # Separate from the shared command/mutation lease: only scheduled writers
        # of one pack serialize. Inherit both leases so an orphan provider cannot
        # overlap a replacement host after a host crash.
        lock_key = hashlib.sha256(provider_id.encode()).hexdigest()[:24]
        scheduled_lease = resources.enter_context(profile_lock(
            root / 'state/background-execution' / lock_key,
            busy_message=f"Scheduled command from '{provider_id}' is still running"))
        lease = resources.enter_context(command_execution_lock(root, key=provider_id, shared=True,
            busy_message=f"Pack '{provider_id}' is being changed"))
        with profile_lock(root):
            current = {task_key(c): (c, s) for c, s in tasks(root)}.get(key)
            if current is None:
                return None
            command, schedule = current
            fingerprint = task_fingerprint(command, schedule)
            if fingerprint != expected_fingerprint:
                return None
            state = read_state(root)
            previous = state['jobs'].get(key, {})
            now = time.time()
            if previous.get('fingerprint') == fingerprint and previous.get('next_at', 0) > now:
                return None
            run_id = hashlib.sha256(f'{key}\0{fingerprint}\0{time.time_ns()}'.encode()).hexdigest()
            row = {'provider': command.provider_id, 'version': command.provider_version,
                   'fingerprint': fingerprint, 'run': run_id,
                   'started_at': now, 'phase': 'running',
                   'next_at': now + schedule['interval_seconds']}
            receipt_dir = root / 'state/background-runs' / run_id
            receipt_dir.mkdir(mode=0o700, parents=True)
            row['receipt_dir'] = str(receipt_dir)
            atomic_json(receipt_dir / 'started.json', {
                'schema': 'tap.background-run/v1', **row, 'command': list(command.path),
                'trigger': {'kind': 'schedule', 'due_at': previous.get('next_at')
                            if previous.get('fingerprint') == fingerprint else None,
                            'schedule': schedule},
                'implementation': {'path': str(command.root),
                                   'manifest_sha256': hashlib.sha256((command.root / 'pack.json').read_bytes()).hexdigest()},
            })
            state['jobs'][key] = row
            atomic_json(root / 'state/background.json', state)
        job = dict(key=key, command=command, schedule=schedule, row=row,
                   process=None, resources=resources, code=None)
        try:
            logdir = root / 'logs/background'
            logdir.mkdir(parents=True, exist_ok=True, mode=0o700)
            logpath = logdir / (hashlib.sha256(key.encode()).hexdigest()[:16] + '.log')
            if logpath.exists() and logpath.stat().st_size > 1024 * 1024:
                logpath.unlink()
            log = resources.enter_context(logpath.open('ab'))
            os.chmod(logpath, 0o600)
            job['process'] = _start_pack(command, root, [], lease_fd=lease.fileno(),
                timeout=schedule['timeout_seconds'], log=log, extra_fds=(scheduled_lease.fileno(),),
                execution={'id': run_id, 'kind': 'schedule', 'receipt_dir': str(receipt_dir)})
            job['deadline'] = time.monotonic() + schedule['timeout_seconds']
        except (TapError, OSError):
            job['code'] = 125
            row['error'] = 'command_start_failed'
        job['resources'] = resources.pop_all()
        return job
    finally:
        resources.close()


def _stop_process(job):
    process = job['process']
    if process is not None:
        # The process may have exited between poll and kill. Its descendants can
        # still hold the inherited leases, so kill the isolated process group.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def _poll_scheduled(job):
    if job['code'] is not None:
        return job['code']
    process = job['process']
    code = process.poll()
    if code is not None:
        return 128 - code if code < 0 else code
    if time.monotonic() >= job['deadline']:
        _stop_process(job)
        return 124
    return None


def _finish_scheduled(root, job, code):
    row = job['row']
    with profile_lock(root):
        state = read_state(root)
        current = state['jobs'].get(job['key'])
        if current is None or current.get('run') != row['run']:
            raise RuntimeError(f"Background run state changed during execution: {job['key']}")
        now = time.time()
        row.update(finished_at=now, exit_code=code,
                   phase='unknown' if code in (124, 130) else 'ok' if code == 0 else 'failed',
                   next_at=now + job['schedule']['interval_seconds'])
        atomic_json(Path(row['receipt_dir']) / 'finished.json', {
            'schema': 'tap.background-run/v1', **row, 'command': list(job['command'].path),
        })
        state['jobs'][job['key']] = row
        atomic_json(root / 'state/background.json', state)
    command = job['command']
    print(stamped('ran %s:%s exit=%s %s' % (
        command.provider_id, '/'.join(command.path), code, row['phase'])), flush=True)


def run_once(root):
    root = Path(root).resolve()
    # launchd already prevents overlap. This also covers manual host invocations.
    with profile_lock(root / 'state/background-host', busy_message='Background host is already running'):
        _run_cycle(root)


def _run_cycle(root):
    running = {}
    active, due = _scheduled_snapshot(root, account_failures=True)
    next_discovery = time.monotonic() + DISCOVERY_SECONDS
    try:
        while True:
            completed = False
            for provider_id, job in list(running.items()):
                code = _poll_scheduled(job)
                if code is None:
                    continue
                job['code'] = code
                try:
                    _finish_scheduled(root, job, code)
                except TapError:
                    # A short lifecycle operation owns the profile; retry the
                    # receipt without rerunning the provider or losing its lease.
                    continue
                job['resources'].close()
                del running[provider_id]
                completed = True
            if completed or time.monotonic() >= next_discovery:
                try:
                    active, due = _scheduled_snapshot(root)
                except TapError:
                    due = []  # Retry fresh discovery, never stale authority.
                next_discovery = time.monotonic() + DISCOVERY_SECONDS
            for _, key, fingerprint, provider_id in due:
                if len(running) >= MAX_RUNNING_PACKS:
                    break
                if provider_id in running:
                    continue
                try:
                    job = _start_scheduled(root, key, fingerprint, provider_id)
                except TapError:
                    continue  # Pack/profile mutation or an inherited orphan lease.
                if job is not None:
                    running[provider_id] = job
            due = []
            if not running:
                break
            time.sleep(POLL_SECONDS)
    finally:
        # SIGTERM/interrupt/exception must reap all children, not leave writers
        # running behind a dead host. Existing timeout/unknown semantics remain.
        for job in running.values():
            try:
                _stop_process(job)
                try:
                    _finish_scheduled(root, job, job['code'] if job['code'] is not None else 130)
                except (TapError, RuntimeError, OSError) as exc:
                    print(stamped(str(exc)), file=sys.stderr, flush=True)
            finally:
                job['resources'].close()
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
