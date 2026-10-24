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
        with command_execution_lock(root, key=expected.provider_id,
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
