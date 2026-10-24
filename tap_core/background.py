"""Profile-owned periodic command execution, independent of capture on/off."""
import hashlib
import json
import math
import os
from pathlib import Path
import plistlib
import signal
import subprocess
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tap_core.runtime import TapError, atomic_json, command_execution_lock, profile_lock, stamped
from tap_core.pack_store import PackStore
from tap_core.packs import PackError, command_schedule
from tap_core.fs_watch import PathWatch, WatchUnavailable, expand
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
            schedule = command_schedule(declaration)
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
        with command_execution_lock(root, key=expected.provider_id,
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


def run_once(root, wake=(), early=0):
    """One scheduling pass. Keys in ``wake`` are due now regardless of their
    next eligible time (a watch trigger fired); everything else keeps its
    interval, or runs up to ``early`` seconds ahead of it so jobs due close
    together share one pass. Returns the active (command, schedule) pairs."""
    root = Path(root).resolve()
    wake = frozenset(wake)
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
            if key not in wake and previous.get('fingerprint') == fingerprint \
                    and previous.get('next_at', 0) > now + early:
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
                if key not in wake and previous.get('fingerprint') == fingerprint \
                        and previous.get('next_at', 0) > now + early:
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
    return active


# Resident host (launchd KeepAlive). It replaces the 10-second interpreter
# tick: it sleeps until the earliest interval deadline, a watch trigger, or
# RESIDENT_MAX_SLEEP_SECONDS (which bounds pickup of pack changes).
RESIDENT_MAX_SLEEP_SECONDS = 30
RESIDENT_COALESCE_SECONDS = 2   # interval jobs due this close together share a pass
TRIGGER_MAX_DELAY_SECONDS = 30  # a change that never quiets still runs
WATCH_FALLBACK_SECONDS = 60     # trigger cadence when the OS watch is unavailable
_CODE = Path(__file__).resolve().parent


def _code_mark():
    """Installed core code identity; a change makes the host exit for restart."""
    return tuple(sorted((path.name, path.stat().st_mtime_ns) for path in _CODE.glob('*.py')))


def _host_path(root):
    return Path(root) / 'state/background-host.json'


def _trigger_ready(trigger, pending, previous, now_wall, now):
    """Earliest monotonic time a pending trigger may run (debounce, then floor)."""
    first, last = pending
    quiet = min(last + trigger['debounce_ms'] / 1000, first + TRIGGER_MAX_DELAY_SECONDS)
    finished = previous.get('finished_at') or previous.get('started_at')
    floor = now + max(0.0, finished + trigger['min_interval_seconds'] - now_wall) if finished else now
    return max(quiet, floor)


def serve(root, stop=None, watch_factory=PathWatch):
    """Run the scheduler resident, with watch triggers, until ``stop`` is set.

    Watches are host observations only: a change marks its task pending; the
    pass that runs it is the same `run_once` used by the periodic tick.
    """
    root = Path(root).resolve()
    stop = stop or threading.Event()
    changed = threading.Event()
    guard = threading.Lock()
    pending = {}     # task key -> (first, last) monotonic event times
    watches = {}     # task key -> (paths, watcher or None, error)
    triggers = {}
    fallback_at = {}
    mark = _code_mark()
    started_at = time.time()

    def changed_paths(key):
        def mark_pending(_paths):
            now = time.monotonic()
            with guard:
                first, _ = pending.get(key, (now, now))
                pending[key] = (first, now)
            changed.set()
        return mark_pending

    try:
        while not stop.is_set():
            if _code_mark() != mark:
                print(stamped('background: core code changed; exiting for restart'), flush=True)
                return
            now, now_wall = time.monotonic(), time.time()
            jobs = read_state(root).get('jobs', {})
            with guard:
                for key in [k for k in pending if k not in triggers]:
                    del pending[key]
                for key, (_, watcher, _) in watches.items():
                    if watcher is None and key in triggers and now >= fallback_at.get(key, 0):
                        pending.setdefault(key, (now, now))
                        fallback_at[key] = now + WATCH_FALLBACK_SECONDS
                ready = {key for key, times in pending.items()
                         if _trigger_ready(triggers[key], times, jobs.get(key, {}), now_wall, now) <= now}
                for key in ready:
                    del pending[key]
            try:
                active = run_once(root, wake=ready, early=RESIDENT_COALESCE_SECONDS)
            except TapError as exc:
                # Busy profile or command lease: keep the wakes and retry soon.
                print(stamped(str(exc)), file=sys.stderr, flush=True)
                with guard:
                    for key in ready:
                        pending.setdefault(key, (now, now))
                stop.wait(2)
                continue
            triggers = {task_key(command): schedule['trigger']
                        for command, schedule in active if 'trigger' in schedule}
            for key in list(watches):
                paths, watcher, _ = watches[key]
                wanted = triggers.get(key)
                if wanted is None or tuple(expand(p, root) for p in wanted['watch']) != paths:
                    if watcher is not None:
                        watcher.close()
                    del watches[key]
            for key, trigger in triggers.items():
                if key in watches:
                    continue
                paths = tuple(expand(p, root) for p in trigger['watch'])
                try:
                    watches[key] = (paths, watch_factory(paths, changed_paths(key)).start(), None)
                except (WatchUnavailable, OSError) as error:
                    watches[key] = (paths, None, str(error))
                    print(stamped('background: watch unavailable for %s (%s); polling every %ds' % (
                        key, error, WATCH_FALLBACK_SECONDS)), file=sys.stderr, flush=True)
            atomic_json(_host_path(root), {
                'version': 1, 'mode': 'resident', 'pid': os.getpid(), 'started_at': started_at,
                'updated_at': time.time(),
                'watches': {key: {'paths': list(paths), 'state': 'watching' if watcher else 'polling',
                                  'error': error}
                            for key, (paths, watcher, error) in sorted(watches.items())}})

            # Sleep until the earliest interval deadline, pending trigger or bound.
            now, now_wall = time.monotonic(), time.time()
            state = read_state(root)
            jobs = state.get('jobs', {})
            deadline = now + RESIDENT_MAX_SLEEP_SECONDS
            for command, schedule in active:
                if freshness_adapter(command) is not None:
                    continue
                next_at = jobs.get(task_key(command), {}).get('next_at', 0)
                deadline = min(deadline, now + max(0.0, next_at - now_wall))
            if any(freshness_adapter(command) for command, _ in active):
                wake_at = state.get('usage_freshness', {}).get('next_wake_at', 0)
                deadline = min(deadline, now + max(0.0, wake_at - now_wall))
            with guard:
                for key, times in pending.items():
                    if key in triggers:
                        deadline = min(deadline, _trigger_ready(
                            triggers[key], times, jobs.get(key, {}), now_wall, now))
                for key, (_, watcher, _) in watches.items():
                    if watcher is None:
                        deadline = min(deadline, fallback_at.get(key, now))
            deadline = max(deadline, time.monotonic() + 0.2)
            # One-second slices only so an embedding caller's `stop` is seen;
            # under launchd SIGTERM interrupts the wait directly.
            while not stop.is_set() and not changed.wait(min(1.0, deadline - time.monotonic())):
                if time.monotonic() >= deadline:
                    break
            changed.clear()
    finally:
        for _, watcher, _ in watches.values():
            if watcher is not None:
                watcher.close()


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
        _host_path(root).unlink(missing_ok=True)  # no host: no stale watch report
        return
    log = root / 'logs/background-host.log'
    log.parent.mkdir(parents=True, exist_ok=True)
    # Resident host: launchd keeps it alive (and restarts it after a code
    # change exit, throttled), instead of starting an interpreter every 10 s.
    value = {'Label': label, 'ProgramArguments': [sys.executable, '-B', str(Path(__file__).resolve()),
                                                  str(root), '--resident'],
             'WorkingDirectory': str(root), 'RunAtLoad': True, 'KeepAlive': True, 'ThrottleInterval': 10,
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
    result = {'registered': plist.exists(), 'label': label, 'quarantined': quarantined, **state}
    try:
        # Last observation written by the resident host; not proof it is alive.
        result['host'] = json.loads(_host_path(root).read_text())
    except (OSError, ValueError):
        pass
    return result


if __name__ == '__main__':
    os.umask(0o077)
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        if sys.argv[2:] == ['--resident']:
            try:
                serve(Path(sys.argv[1]))
            except KeyboardInterrupt:
                pass
        else:
            run_once(Path(sys.argv[1]))
    except TapError as exc:
        # Busy is expected: next tick retries discovery, never stale pack code.
        print(stamped(str(exc)), file=sys.stderr, flush=True)
        sys.exit(1)
