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
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tap_core.runtime import TapError, atomic_json, command_execution_lock, profile_lock, stamped
from tap_core.pack_store import PackStore
from tap_core.packs import PackError, command_schedule
from tap_core.fs_watch import PathWatch, WatchUnavailable, expand
from tap_core.commands import discover, _run_pack, _start_pack
from tap_core import freshness_policy


def paths(root):
    key = hashlib.sha256(str(Path(root).resolve()).encode()).hexdigest()[:16]
    label = 'com.tap.core.background.' + key
    return label, Path.home() / 'Library/LaunchAgents' / (label + '.plist')


VERIFY_QUARANTINE_THRESHOLD = 5
FRESHNESS_WAKE_SECONDS = 60

# Transitional binding until #228 supplies a pack-declared adapter contract.
# 0.22.0 requires a selected client; older collectors ignore argv and would
# poll everyone. Never invoke them through this coordinator.
_USAGE_DATA = ("data", "readers", "usage.meters")
FRESHNESS_ADAPTERS = {
    ("usage.meters", ("usage", "collect")): {
        "minimum_version": (0, 22, 0),
        "targets": tuple(
            {"provider": client,
             "freshness": (*_USAGE_DATA, "freshness.json"),
             "passive": (*_USAGE_DATA, "capture-freshness.json"),
             "activity": {"path": (*_USAGE_DATA, "native-activity.jsonl" if client == "antigravity" else "activity.jsonl"),
                          "client": "antigravity-hub" if client == "antigravity" else client}}
            for client in ("codex", "antigravity", "cursor", "factory", "openrouter")
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


def _passive_observation(root, target, now):
    """Read the pack's complete capture observation, never history write time.

    The pack owns coverage (including multi-endpoint quotas). The coordinator
    rejects ambiguous account scope and expired windows before applying its age
    policy. This index is separate from the display's live-look sidecar.
    """
    if not target.get("passive"):
        return None
    try:
        payload = json.loads(Path(root).joinpath(*target["passive"]).read_text())
        if payload.get("schema") != "tap.usage-passive-quota/v1":
            return None
        scopes = payload["clients"][target["provider"]]
        if not isinstance(scopes, dict) or len(scopes) != 1:
            return None
        row = next(iter(scopes.values()))
        ts, until = row.get("observed_at"), row.get("valid_until")
        if type(ts) not in (int, float) or not math.isfinite(ts) or ts <= 0 or ts > now + 300:
            return None
        if until is not None and (type(until) not in (int, float) or not math.isfinite(until)):
            return None
        try:
            live = json.loads(Path(root).joinpath(*target["freshness"]).read_text())
            account = (live.get("accounts") or {}).get(target["provider"])
        except (OSError, ValueError, AttributeError):
            account = None
        if account is not None and row.get("account") != account:
            return None
        return row
    except (OSError, ValueError, KeyError, AttributeError, TypeError):
        return None


def _passive_at(root, target, now):
    row = _passive_observation(root, target, now)
    if row is None or (row.get("valid_until") is not None and now >= row["valid_until"]):
        return None
    return row["observed_at"]


def _quota_observations(root, spec, target, now):
    # Rebuild a prior passive hint from current evidence: an account switch or
    # corrupt index must not leave a historical hint suppressing refresh forever.
    if target.get("last_authoritative_via") == "passive-capture":
        target = dict(target, last_authoritative_at=None, last_authoritative_via=None,
                      last_authoritative_until=None)
    target = freshness_policy.observed_quota(target, _freshness_at(root, spec), "live-adapter", now)
    row = _passive_observation(root, spec, now)
    if row:
        target = freshness_policy.observed_quota(target, row["observed_at"], "passive-capture", now,
                                                 valid_until=row.get("valid_until"))
    return target


def _selective_adapter(command, adapter):
    try:
        return tuple(int(p) for p in command.provider_version.split(".")) >= adapter["minimum_version"]
    except (ValueError, AttributeError):
        return False


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
    """Decide and execute per client in the existing finite command host."""
    root = Path(root).resolve()
    admitted = [(command, freshness_adapter(command)) for command, _ in commands
                if freshness_adapter(command) is not None]
    if not admitted:
        return []
    now = time.time()
    state = read_state(root)
    account = _freshness_account(state)
    if account.get("selector_version") == 1 and account.get("next_wake_at", 0) > now:
        return []
    policy_states, metadata = [], []
    for command, adapter in admitted:
        if not _selective_adapter(command, adapter):
            # Fail closed during upgrade/rollback, retaining budgets/backoff.
            continue
        for spec in adapter["targets"]:
            target = _restore_target(account, command, spec)
            seen = _freshness_at(root, spec)
            target = _quota_observations(root, spec, target, now)
            target = freshness_policy.observed_activity(target, _activity_at(root, spec), now)
            policy_states.append(target)
            metadata.append((_target_key(command, spec), command, spec, seen))
    context = freshness_policy.context(now, utc_offset_seconds=_local_utc_offset_seconds(now), online=None)
    policy_states, receipts = freshness_policy.plan(policy_states, context)
    account["targets"].update({key: target for (key, _, _, _), target in zip(metadata, policy_states)})
    for command, adapter in admitted:
        if _selective_adapter(command, adapter):
            account["targets"].pop(_adapter_id(command), None)
    account["selector_version"] = 1
    account["next_wake_at"] = now + FRESHNESS_WAKE_SECONDS
    atomic_json(root / "state/background.json", state)
    _append_freshness_receipts(root, receipts)

    for index, decision in enumerate(receipts):
        if decision["action"] != freshness_policy.REFRESH:
            continue
        key, expected, spec, previous = metadata[index]
        with command_execution_lock(root, key=expected.provider_id, shared=True,
                                    busy_message=f"A command from pack '{expected.provider_id}' is still running") as lease:
            current = {_adapter_id(command): command for command, _ in tasks(root)}.get(_adapter_id(expected))
            if current is None or current.provider_version != expected.provider_version:
                continue
            state = read_state(root)
            account = _freshness_account(state)
            started_at = time.time()
            target = account["targets"].get(key, policy_states[index])
            # A response may have arrived while obtaining the execution lease.
            target = _quota_observations(root, spec, target, started_at)
            fresh_context = dict(context, now=started_at)
            checked = freshness_policy.decide(target, fresh_context)
            if checked["action"] != freshness_policy.REFRESH:
                account["targets"][key] = target
                atomic_json(root / "state/background.json", state)
                _append_freshness_receipts(root, [checked])
                continue
            account["targets"][key] = freshness_policy.started(target, decision, started_at)
            atomic_json(root / "state/background.json", state)
            try:
                code = _run_pack(current, root,
                                 [spec["provider"], "--max-age", str(checked["inputs"]["interval"])],
                                 lease_fd=lease.fileno(), timeout=60)
            except (TapError, OSError):
                code = 125
            finished_at = time.time()
            state = read_state(root)
            account = _freshness_account(state)
            target = account["targets"].get(key)
            if target is None:
                continue
            observed = _freshness_at(root, spec)
            moved = code == 0 and type(observed) in (int, float) and math.isfinite(observed) \
                and (previous is None or observed > previous)
            passive = _passive_at(root, spec, finished_at)
            passive_fresh = code == 0 and passive is not None \
                and finished_at - passive < checked["inputs"]["interval"]
            if moved:
                target = freshness_policy.finished(target, "unchanged", finished_at, quota_observed_at=observed)
                outcome = "unchanged" if target.get("last_error") is None else "error"
            elif passive_fresh:
                target = freshness_policy.finished(target, "unchanged", finished_at, quota_observed_at=passive)
                target["last_authoritative_via"] = "passive-capture"
                target["last_authoritative_until"] = (_passive_observation(root, spec, finished_at) or {}).get("valid_until")
                outcome = "passive_fresh"
            else:
                target = freshness_policy.finished(target, "error", finished_at)
                outcome = "error"
            account["targets"][key] = target
            atomic_json(root / "state/background.json", state)
            _append_freshness_receipts(root, [{
                "receipt": "tap.usage-freshness-execution/v1", "decision_id": decision["decision_id"],
                "at": finished_at, "target": decision["target"], "exit_code": code,
                "outcome": outcome, "ride_along": False,
            }])
    return receipts


def _effective_interval(schedule):
    floor = int(os.environ.get('TAP_MIN_SCHEDULE_SECONDS', 0) or 0)
    return max(schedule['interval_seconds'], floor)
# One host, existing provider subprocesses, no additional service or queue store.
MAX_RUNNING_PACKS = int(os.environ.get('TAP_MAX_RUNNING_PACKS', 4) or 4)
DISCOVERY_SECONDS = 10
POLL_SECONDS = 0.1


def _scheduled_snapshot(root, *, account_failures=False, wake=frozenset(), early=0):
    """Due scheduled jobs. Keys in ``wake`` are due now (a watch trigger fired);
    others are due at their next time, or up to ``early`` seconds ahead of it so
    jobs due close together start in one pass."""
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
        if key in wake or next_at <= time.time() + early:
            due.append((next_at, key, fingerprint, command.provider_id))
    state['checked_at'] = time.time()
    atomic_json(root / 'state/background.json', state)
    # Oldest due first: a fast recurring command cannot jump an overdue neighbour.
    return active, sorted(due, key=lambda item: item[0])


def _start_scheduled(root, key, expected_fingerprint, provider_id, *, woken=False, early=0):
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
        if not woken and previous.get('fingerprint') == fingerprint and previous.get('next_at', 0) > now + early:
            return None
        run_id = hashlib.sha256(f'{key}\0{fingerprint}\0{time.time_ns()}'.encode()).hexdigest()
        row = {'provider': command.provider_id, 'version': command.provider_version,
               'fingerprint': fingerprint, 'run': run_id,
               'started_at': now, 'phase': 'running',
               'next_at': now + _effective_interval(schedule)}
        receipt_dir = root / 'state/background-runs' / run_id
        receipt_dir.mkdir(mode=0o700, parents=True)
        row['receipt_dir'] = str(receipt_dir)
        atomic_json(receipt_dir / 'started.json', {
            'schema': 'tap.background-run/v1', **row, 'command': list(command.path),
            'trigger': {'kind': 'watch' if woken else 'schedule', 'due_at': previous.get('next_at')
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
    state = read_state(root)
    current = state['jobs'].get(job['key'])
    if current is None or current.get('run') != row['run']:
        raise RuntimeError(f"Background run state changed during execution: {job['key']}")
    now = time.time()
    row.update(finished_at=now, exit_code=code,
               phase='unknown' if code in (124, 130) else 'ok' if code == 0 else 'failed',
               next_at=now + _effective_interval(job['schedule']))
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


def _run_cycle(root, take_wake=None, early=0):
    """Start due jobs (up to MAX_RUNNING_PACKS packs at once) and wait for them.

    ``take_wake()`` returns trigger keys that became ready; it is consulted at
    every discovery point, so a watch firing while jobs run starts its task in
    the same cycle. Returns (active tasks, woken keys that did not start)."""
    running = {}
    wake = set(take_wake()) if take_wake else set()
    active, due = _scheduled_snapshot(root, account_failures=True, wake=frozenset(wake), early=early)
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
                if take_wake:
                    wake |= set(take_wake())
                try:
                    active, due = _scheduled_snapshot(root, wake=frozenset(wake), early=early)
                except TapError:
                    due = []  # Retry fresh discovery, never stale authority.
                next_discovery = time.monotonic() + DISCOVERY_SECONDS
            for _, key, fingerprint, provider_id in due:
                if len(running) >= MAX_RUNNING_PACKS:
                    break
                if provider_id in running:
                    continue
                try:
                    job = _start_scheduled(root, key, fingerprint, provider_id,
                                           woken=key in wake, early=early)
                except TapError:
                    continue  # Pack/profile mutation or an inherited orphan lease.
                wake.discard(key)  # started, or no longer startable at this fingerprint
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
    return active, wake


# Resident host (launchd KeepAlive). It replaces the 10-second interpreter
# tick: it sleeps until the earliest interval deadline, a watch trigger, or
# RESIDENT_MAX_SLEEP_SECONDS (which bounds pickup of pack changes).
RESIDENT_MAX_SLEEP_SECONDS = 30
RESIDENT_COALESCE_SECONDS = 2   # interval jobs due this close together share a cycle
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
    same `_run_cycle` used by the periodic tick starts it.
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

    def take_wake():
        now, now_wall = time.monotonic(), time.time()
        jobs = read_state(root).get('jobs', {})
        with guard:
            for key, (_, watcher, _) in watches.items():
                if watcher is None and key in triggers and now >= fallback_at.get(key, 0):
                    pending.setdefault(key, (now, now))
                    fallback_at[key] = now + WATCH_FALLBACK_SECONDS
            ready = {key for key, times in pending.items() if key in triggers
                     and _trigger_ready(triggers[key], times, jobs.get(key, {}), now_wall, now) <= now}
            for key in ready:
                del pending[key]
        return ready

    with profile_lock(root / 'state/background-host', busy_message='Background host is already running'):
        try:
            while not stop.is_set():
                if _code_mark() != mark:
                    print(stamped('background: core code changed; exiting for restart'), flush=True)
                    return
                try:
                    active, leftover = _run_cycle(root, take_wake, early=RESIDENT_COALESCE_SECONDS)
                except TapError as exc:
                    # Busy profile: retry soon; pending triggers stay pending.
                    print(stamped(str(exc)), file=sys.stderr, flush=True)
                    stop.wait(2)
                    continue
                triggers = {task_key(command): schedule['trigger']
                            for command, schedule in active if 'trigger' in schedule}
                now = time.monotonic()
                with guard:
                    for key in [k for k in pending if k not in triggers]:
                        del pending[key]
                    for key in leftover & set(triggers):
                        pending.setdefault(key, (now, now))  # e.g. its pack's lease was busy
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
                # A floor keeps a busy lease or a past deadline from spinning.
                deadline = max(deadline, time.monotonic() + (1.0 if leftover else 0.2))
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


BOOTOUT_WAIT_SECONDS = 10


def _wait_unloaded(adapter, target):
    """launchd keeps reporting a booted-out job while it is still exiting (a
    running tick gets SIGTERM first). Bootstrapping is only decided once the old
    job is gone, or a replaced agent would be left unloaded."""
    deadline = time.monotonic() + BOOTOUT_WAIT_SECONDS
    while adapter.run(['/bin/launchctl', 'print', target], check=False).returncode == 0:
        if time.monotonic() >= deadline:
            raise TapError(f'Previous background agent did not unload within {BOOTOUT_WAIT_SECONDS}s; '
                           'retry to finish registration')
        time.sleep(0.2)


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
    replaced = False
    if plist.exists() and plist.read_bytes() != data:
        result = adapter.run(['/bin/launchctl', 'print', target], check=False)
        if result.returncode == 0:
            adapter.run(['/bin/launchctl', 'bootout', target])
            replaced = True
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_bytes(data)
    plist.chmod(0o600)
    if replaced:
        _wait_unloaded(adapter, target)
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
