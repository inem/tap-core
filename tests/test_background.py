import contextlib
import copy
import io
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import sys
import threading
import time
import unittest
from unittest.mock import patch, Mock
from tap_core import background
from tap_core.pack_store import PackStore, build_artifact
from tap_core.packs import validate_manifest, PackError
from tap_core.runtime import TapError, command_execution_lock, profile_lock, stamped

STAMP = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z ")

class BackgroundTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'
        shutil.copytree(Path(__file__).resolve().parent.parent / 'fixtures/packs/command', self.source)
        self.manifest = json.loads((self.source / 'pack.json').read_text())
        self.manifest['entrypoints']['command']['commands'] = [self.manifest['entrypoints']['command']['commands'][0]]
        self.manifest['entrypoints']['command']['commands'][0]['schedule'] = {'interval_seconds': 30, 'timeout_seconds': 1}
        self.manifest['access']['capabilities'].append('background.run')
        (self.source / 'command.py').write_text('import os,json,pathlib\nc=json.loads(os.environ["TAP_COMMAND_CONTEXT"])\np=pathlib.Path(c["output_dir"])/"runs"\nwith p.open("a") as f: f.write(c["provider"]["version"]+"\\n")\n')
        self.profile = self.root / 'profile'
        self.store = PackStore(self.profile)

    def install(self, version='0.1.0'):
        self.manifest['version'] = version
        (self.source / 'pack.json').write_text(json.dumps(self.manifest))
        artifact = self.root / (version + '.tap-pack')
        build_artifact(self.source, artifact)
        self.store.install(artifact)
        self.store.enable('fixture.command', version, origins=['https://fixture.example'], capabilities=['command.execute', 'background.run'])

    def install_usage_adapter(self, version='0.1.0'):
        source = self.root / 'usage-source'
        shutil.copytree(self.source, source)
        manifest = json.loads((source / 'pack.json').read_text())
        manifest['id'] = 'usage.meters'
        manifest['version'] = version
        declaration = manifest['entrypoints']['command']['commands'][0]
        declaration.update({'path': ['usage', 'collect'], 'summary': 'collect usage', 'usage': '(no arguments)',
                            'schedule': {'interval_seconds': 180, 'timeout_seconds': 60}})
        manifest['access']['capabilities'].append('background.run')
        (source / 'pack.json').write_text(json.dumps(manifest))
        artifact = self.root / ('usage-' + version + '.tap-pack')
        build_artifact(source, artifact)
        self.store.install(artifact)
        self.store.enable('usage.meters', version, origins=['https://fixture.example'],
                          capabilities=['command.execute', 'background.run'])

    def write_usage_freshness(self, observed_at, **per_provider):
        """The pack's sidecar after a live look: one key per provider it asked.
        Both admitted targets move together unless a provider is overridden."""
        observed = {'codex': observed_at, 'antigravity': observed_at}
        observed.update(per_provider)
        path = self.profile / 'data/readers/usage.meters/freshness.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'version': 1, 'observed_at': {
            k: v for k, v in observed.items() if v is not None}}))

    def write_usage_activity(self, rows):
        path = self.profile / 'data/readers/usage.meters/native-activity.jsonl'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(''.join(
            (row if isinstance(row, str) else json.dumps(row)) + '\n' for row in rows))

    def freshness_targets(self):
        return background.read_state(self.profile)['usage_freshness']['targets']

    def execution_receipts(self):
        path = self.profile / 'logs/usage-freshness-decisions.jsonl'
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        return [r for r in rows if r['receipt'] == 'tap.usage-freshness-execution/v1']

    def test_due_selected_version_disable_and_retained_data_without_capture(self):
        self.install()
        background.run_once(self.profile)
        background.run_once(self.profile)
        runs = self.profile / 'data/packs/fixture.command/runs'
        self.assertEqual(runs.read_text().splitlines(), ['0.1.0'])
        self.assertFalse((self.profile / 'profile.json').exists())
        self.install('0.2.0')
        background.run_once(self.profile)
        self.assertEqual(runs.read_text().splitlines(), ['0.1.0', '0.2.0'])
        self.store.disable('fixture.command')
        background.run_once(self.profile)
        self.assertEqual(background.read_state(self.profile)['jobs'], {})
        self.store.uninstall('fixture.command')
        self.assertTrue(runs.exists())

    def test_timeout_is_unknown_and_releases_lease(self):
        (self.source / 'command.py').write_text('import time\ntime.sleep(30)\n')
        self.install()
        background.run_once(self.profile)
        row = next(iter(background.read_state(self.profile)['jobs'].values()))
        self.assertEqual(row['phase'], 'unknown')
        self.assertEqual(row['exit_code'], 124)
        self.store.disable('fixture.command')
        background.run_once(self.profile)

    def test_freshness_coordinator_only_runs_when_a_target_is_due(self):
        self.install_usage_adapter()
        now = 1_000_000.0
        self.write_usage_freshness(now)
        commands = background.tasks(self.profile)
        with patch.object(background.time, 'time', return_value=now), \
                patch.object(background, '_run_pack') as run:
            receipts = background.run_freshness_once(self.profile, commands)
        self.assertEqual({r['target']['provider'] for r in receipts}, {'codex', 'antigravity'})
        self.assertEqual({r['reason'] for r in receipts}, {'idle_decay'})
        run.assert_not_called()

        later = now + background.freshness_policy.DEFAULT_POLICY['idle_interval'] + 1
        def collect(*_args, **_kwargs):
            self.write_usage_freshness(later)
            return 0

        with patch.object(background.time, 'time', return_value=later), \
                patch.object(background, '_run_pack', side_effect=collect) as run:
            receipts = background.run_freshness_once(self.profile, commands)
        # Both targets are due; one wake approves one of them, the other is
        # coalesced, and the single run serves both.
        verdicts = {r['target']['provider']: (r['action'], r['reason']) for r in receipts}
        self.assertEqual(sorted(verdicts.values()), [('refresh', 'due_idle'), ('skip', 'coalesced_wake')])
        run.assert_called_once()
        targets = self.freshness_targets()
        self.assertEqual(set(targets), {'usage.meters:usage collect/codex',
                                        'usage.meters:usage collect/antigravity'})
        for target in targets.values():
            self.assertEqual((target['last_authoritative_at'], target['failures'],
                              target['last_authoritative_via']), (later, 0, 'live-adapter'))
        executions = self.execution_receipts()
        self.assertEqual(sorted((r['target']['provider'], r['ride_along'], r['outcome']) for r in executions),
                         sorted([(p, verdicts[p][0] != 'refresh', 'unchanged') for p in verdicts]))
        approved = next(r for r in receipts if r['action'] == 'refresh')
        self.assertTrue(all(r['decision_id'] == approved['decision_id'] for r in executions))

    def test_antigravity_activity_newer_than_its_snapshot_makes_it_active(self):
        # Hub activity after the last look moves the Antigravity target to the
        # 15-minute tier; Codex, with no activity source, stays idle and is not
        # even due. The one run still refreshes both keys.
        self.install_usage_adapter()
        now = 1_000_000.0
        looked = now - 1000
        self.write_usage_freshness(looked)
        self.write_usage_activity([
            {'schema': 2, 'record': 'activity', 'client': 'gemini-cli', 'observed_at': now - 10},
            'not json at all',
            {'schema': 2, 'record': 'activity', 'client': 'antigravity-hub', 'observed_at': now - 300},
            {'schema': 2, 'record': 'activity', 'client': 'antigravity-hub', 'observed_at': now - 900},
        ])
        commands = background.tasks(self.profile)

        def collect(*_args, **_kwargs):
            self.write_usage_freshness(now)
            return 0

        with patch.object(background.time, 'time', return_value=now), \
                patch.object(background, '_run_pack', side_effect=collect) as run:
            receipts = background.run_freshness_once(self.profile, commands)
        by = {r['target']['provider']: r for r in receipts}
        self.assertEqual((by['antigravity']['tier'], by['antigravity']['action'], by['antigravity']['reason']),
                         ('active', 'refresh', 'due_active'))
        self.assertEqual(by['antigravity']['inputs']['last_activity_at'], now - 300)
        self.assertEqual((by['codex']['tier'], by['codex']['action'], by['codex']['reason']),
                         ('idle', 'skip', 'idle_decay'))
        self.assertIsNone(by['codex']['inputs']['last_activity_at'])
        run.assert_called_once()
        targets = self.freshness_targets()
        self.assertEqual(targets['usage.meters:usage collect/antigravity']['last_authoritative_at'], now)
        self.assertEqual(targets['usage.meters:usage collect/codex']['last_authoritative_at'], now)
        self.assertEqual([(r['target']['provider'], r['ride_along']) for r in self.execution_receipts()],
                         [('codex', True), ('antigravity', False)])

    def test_activity_the_snapshot_already_covers_keeps_antigravity_idle(self):
        self.install_usage_adapter()
        now = 1_000_000.0
        self.write_usage_freshness(now - 1000)
        self.write_usage_activity([
            {'schema': 2, 'record': 'activity', 'client': 'antigravity-hub', 'observed_at': now - 2000}])
        commands = background.tasks(self.profile)
        with patch.object(background.time, 'time', return_value=now), \
                patch.object(background, '_run_pack') as run:
            receipts = background.run_freshness_once(self.profile, commands)
        by = {r['target']['provider']: r for r in receipts}
        self.assertEqual((by['antigravity']['tier'], by['antigravity']['reason']), ('idle', 'idle_decay'))
        self.assertEqual(by['antigravity']['inputs']['last_activity_at'], now - 2000)
        run.assert_not_called()

    def test_activity_tail_reads_the_newest_row_of_the_client_only(self):
        target = background.FRESHNESS_ADAPTERS[('usage.meters', ('usage', 'collect'))]['targets'][1]
        self.assertIsNone(background._activity_at(self.profile, target))
        rows = [{'schema': 2, 'record': 'activity', 'client': 'antigravity-hub', 'observed_at': 500.0}]
        self.write_usage_activity(rows)
        self.assertEqual(background._activity_at(self.profile, target), 500.0)
        # Order on disk does not matter, other clients and bad rows do not count,
        # and a non-finite time is not "newest".
        self.write_usage_activity([
            {'schema': 2, 'record': 'activity', 'client': 'antigravity-hub', 'observed_at': 700.0},
            {'schema': 2, 'record': 'activity', 'client': 'antigravity-hub', 'observed_at': 600.0},
            {'schema': 2, 'record': 'activity', 'client': 'gemini-cli', 'observed_at': 900.0},
            {'schema': 2, 'record': 'activity', 'client': 'antigravity-hub', 'observed_at': 'Infinity'},
            '{"client": "antigravity-hub", "observed_at": Infinity}',
            '{broken',
        ])
        self.assertEqual(background._activity_at(self.profile, target), 700.0)
        # Only the tail is read: a huge ledger whose newest row is at the end.
        filler = json.dumps({'schema': 2, 'record': 'activity', 'client': 'antigravity-hub',
                             'observed_at': 1.0, 'pad': 'x' * 200})
        self.write_usage_activity([filler] * (2 * background.ACTIVITY_TAIL_BYTES // len(filler))
                                  + [{'schema': 2, 'record': 'activity', 'client': 'antigravity-hub',
                                      'observed_at': 800.0}])
        self.assertEqual(background._activity_at(self.profile, target), 800.0)
        self.assertIsNone(background._activity_at(self.profile, {'provider': 'codex'}))

    def test_legacy_single_target_state_is_adopted_by_the_codex_target(self):
        self.install_usage_adapter()
        now = 1_000_000.0
        self.write_usage_freshness(now)
        state = background.read_state(self.profile)
        legacy = background.freshness_policy.new_state('codex', adapter='usage.meters:usage collect')
        legacy['attempts'] = [now - 100]
        state['usage_freshness'] = {'version': 1, 'next_wake_at': 0,
                                    'targets': {'usage.meters:usage collect': legacy}}
        background.atomic_json(self.profile / 'state/background.json', state)
        with patch.object(background.time, 'time', return_value=now), \
                patch.object(background, '_run_pack') as run:
            receipts = background.run_freshness_once(self.profile, background.tasks(self.profile))
        run.assert_not_called()
        targets = self.freshness_targets()
        self.assertNotIn('usage.meters:usage collect', targets)
        self.assertEqual(targets['usage.meters:usage collect/codex']['attempts'], [now - 100])
        self.assertEqual(targets['usage.meters:usage collect/antigravity']['attempts'], [])
        self.assertEqual({r['target']['provider']: r['inputs']['budget_used'] for r in receipts},
                         {'codex': 1, 'antigravity': 0})

    def test_idle_refresh_has_slack_before_the_usage_view_turns_stale(self):
        # client-usage shows "seen ... ago" at six hours.  The coordinator
        # checks every 15 minutes and grants the adapter its 60-second timeout,
        # so its idle deadline must leave enough room for both.
        presentation_stale_after = 6 * 3600
        latest_possible_refresh = (
            background.freshness_policy.DEFAULT_POLICY['idle_interval']
            + background.FRESHNESS_WAKE_SECONDS
            + background.freshness_policy.DEFAULT_POLICY['timeout']
        )
        self.assertLess(latest_possible_refresh, presentation_stale_after)

    def test_freshness_coordinator_backs_off_only_the_target_that_asked(self):
        # No sidecar at all: both targets are due, one is approved, the run
        # reports no quota. The approved target backs off; the neighbour it was
        # meant to serve is recorded as a failed ride-along but keeps its budget.
        self.install_usage_adapter()
        now = 1_000_000.0
        commands = background.tasks(self.profile)
        with patch.object(background.time, 'time', return_value=now), \
                patch.object(background, '_run_pack', return_value=0) as run:
            receipts = background.run_freshness_once(self.profile, commands)
        run.assert_called_once()
        approved = next(r for r in receipts if r['action'] == 'refresh')
        other = next(r for r in receipts if r['action'] != 'refresh')
        self.assertEqual((approved['reason'], other['reason']), ('due_idle', 'coalesced_wake'))
        targets = self.freshness_targets()
        asked = targets['usage.meters:usage collect/' + approved['target']['provider']]
        neighbour = targets['usage.meters:usage collect/' + other['target']['provider']]
        self.assertEqual((asked['failures'], asked['last_error']), (1, 'error'))
        self.assertEqual(asked['retry_at'], now + background.freshness_policy.DEFAULT_POLICY['backoff']['base'])
        self.assertEqual((neighbour['failures'], neighbour['retry_at'], neighbour['attempts']), (0, None, []))
        self.assertEqual(sorted((r['ride_along'], r['outcome']) for r in self.execution_receipts()),
                         [(False, 'error'), (True, 'error')])

    def test_ride_along_success_clears_the_neighbours_backoff(self):
        # Antigravity is backing off after a failed look of its own; Codex's
        # approved run moves the Antigravity key too. That is a successful look
        # for Antigravity: streak cleared, snapshot time advanced, no attempt spent.
        self.install_usage_adapter()
        now = 1_000_000.0
        policy = background.freshness_policy.DEFAULT_POLICY
        self.write_usage_freshness(now - policy['idle_interval'] - 1, antigravity=now - 60)
        state = background.read_state(self.profile)
        stuck = background.freshness_policy.new_state('antigravity', adapter='usage.meters:usage collect')
        stuck.update(failures=3, retry_at=now + 3600, last_error='timeout')
        state['usage_freshness'] = {'version': 1, 'next_wake_at': 0,
                                    'targets': {'usage.meters:usage collect/antigravity': stuck}}
        background.atomic_json(self.profile / 'state/background.json', state)

        def collect(*_args, **_kwargs):
            self.write_usage_freshness(now)
            return 0

        with patch.object(background.time, 'time', return_value=now), \
                patch.object(background, '_run_pack', side_effect=collect) as run:
            receipts = background.run_freshness_once(self.profile, background.tasks(self.profile))
        by = {r['target']['provider']: r for r in receipts}
        self.assertEqual((by['codex']['action'], by['antigravity']['reason']), ('refresh', 'backoff'))
        run.assert_called_once()
        targets = self.freshness_targets()
        anti = targets['usage.meters:usage collect/antigravity']
        self.assertEqual((anti['failures'], anti['retry_at'], anti['last_error'], anti['attempts']),
                         (0, None, None, []))
        self.assertEqual((anti['last_authoritative_at'], anti['last_authoritative_via']), (now, 'live-adapter'))
        self.assertEqual(targets['usage.meters:usage collect/codex']['attempts'], [now])

    def test_background_does_not_also_run_the_legacy_usage_interval(self):
        self.install_usage_adapter()
        now = 1_000_000.0
        self.write_usage_freshness(now)
        with patch.object(background.time, 'time', return_value=now), \
                patch.object(background, '_run_pack') as run:
            background.run_once(self.profile)
        run.assert_not_called()

    def test_running_job_holds_execution_but_not_lifecycle_lease(self):
        self.install()
        started = threading.Event()
        release = threading.Event()
        errors = []

        def run_pack(*_args, **_kwargs):
            started.set()
            release.wait(2)
            return 0

        def run_background():
            try:
                background.run_once(self.profile)
            except Exception as error:
                errors.append(error)

        with patch.object(background, '_run_pack', side_effect=run_pack):
            worker = threading.Thread(target=run_background)
            worker.start()
            self.assertTrue(started.wait(1))
            # Capture/network lifecycle can take its short profile authority
            # while the provider is doing slow external work.
            with profile_lock(self.profile):
                pass
            # Pack mutation still cannot invalidate the selected provider.
            # The execution lease is scoped to the provider id: the same pack
            # conflicts, and the lease name identifies it.
            with self.assertRaisesRegex(TapError, "still running"):
                with command_execution_lock(self.profile, key='fixture.command'):
                    pass
            # A different pack's lease is unaffected by this run.
            with command_execution_lock(self.profile, key='other.pack'):
                pass
            release.set()
            worker.join(3)

        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])

    def test_execution_lease_scoped_per_pack(self):
        with command_execution_lock(self.profile, key='pack.a'):
            with command_execution_lock(self.profile, key='pack.b'):
                pass  # unrelated packs do not wait on each other
            with self.assertRaisesRegex(TapError, 'still running'):
                with command_execution_lock(self.profile, key='pack.a'):
                    pass

    def test_schedule_requires_permission_and_finite_bounds(self):
        bad = copy.deepcopy(self.manifest)
        bad['access']['capabilities'].remove('background.run')
        with self.assertRaises(PackError): validate_manifest(bad, self.source)
        for value in [True, 0, 86401]:
            bad = copy.deepcopy(self.manifest)
            bad['entrypoints']['command']['commands'][0]['schedule']['interval_seconds'] = value
            with self.assertRaises(PackError): validate_manifest(bad, self.source)

    def test_register_and_remove_last_scheduled_pack(self):
        self.install()
        adapter = Mock()
        adapter.run.return_value.returncode = 1
        plist = self.root / 'job.plist'
        with patch.object(background, 'paths', return_value=('fixture.job', plist)):
            background.reconcile(self.profile, adapter)
            self.assertTrue(plist.exists())
            self.store.disable('fixture.command')
            adapter.run.return_value.returncode = 0
            background.reconcile(self.profile, adapter)
            self.assertFalse(plist.exists())
            self.assertTrue(any('bootout' in call.args[0] for call in adapter.run.call_args_list))

    def install_other(self, version='0.1.0'):
        # A second, independent scheduled pack (distinct id and command path)
        # so a broken neighbour can be observed alongside a healthy one.
        src = self.root / 'other-source'
        if src.exists():
            shutil.rmtree(src)
        shutil.copytree(self.source, src)
        manifest = json.loads((src / 'pack.json').read_text())
        manifest['id'] = 'fixture.other'
        manifest['version'] = version
        manifest['entrypoints']['command']['commands'][0]['path'] = ['other', 'echo']
        (src / 'pack.json').write_text(json.dumps(manifest))
        artifact = self.root / ('other-' + version + '.tap-pack')
        build_artifact(src, artifact)
        self.store.install(artifact)
        self.store.enable('fixture.other', version, origins=['https://fixture.example'],
                          capabilities=['command.execute', 'background.run'])

    def tamper(self, pack_id, version):
        hashes = self.store.load()['packs'][pack_id]['versions'][version]['hashes']
        name = next(n for n in hashes if n != 'pack.json')
        victim = self.store.version_root(pack_id, version) / name
        victim.write_bytes(victim.read_bytes() + b'\n# tampered\n')

    def test_pack_integrity_failure_is_isolated_not_crashing(self):
        # A single installed pack whose on-disk content drifted from its
        # registry hashes must not take down the whole scheduler. (#137)
        self.install()
        self.tamper('fixture.command', '0.1.0')
        # verify() itself still reports the integrity failure...
        with self.assertRaises(PackError):
            self.store.verify(self.store.load(), 'fixture.command', '0.1.0')
        # ...but tasks() isolates it: the bad pack is skipped, no exception.
        self.assertEqual(background.tasks(self.profile), [])
        # ...and run_once completes instead of crash-looping.
        background.run_once(self.profile)

    def test_stamped_prefixes_an_iso8601_utc_timestamp(self):
        line = stamped("hub ready pid=42")
        self.assertRegex(line, STAMP)
        self.assertTrue(line.endswith(" hub ready pid=42"))

    def test_run_logs_a_timestamped_outcome_line(self):
        # #145: the background host's own lines carry a UTC timestamp so "when
        # did this task run" is answerable from background-host.log alone.
        self.install()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            background.run_once(self.profile)
        outcome = [line for line in out.getvalue().splitlines()
                   if "ran fixture.command:" in line]
        self.assertEqual(len(outcome), 1, out.getvalue())
        self.assertRegex(outcome[0], STAMP)
        self.assertIn("exit=0 ok", outcome[0])
    def test_verify_failure_quarantines_after_threshold_and_neighbor_runs(self):
        # #139: a persistently broken pack is quarantined after N failures — it
        # stops being retried and log-spammed — while a healthy neighbour keeps
        # running the whole time.
        self.install()             # healthy fixture.command
        self.install_other()       # neighbour fixture.other
        self.tamper('fixture.other', '0.1.0')
        threshold = background.VERIFY_QUARANTINE_THRESHOLD
        for _ in range(threshold):
            background.run_once(self.profile)
        # The healthy neighbour ran (once, then its 30s interval holds it).
        self.assertEqual((self.profile / 'data/packs/fixture.command/runs').read_text().splitlines(),
                         ['0.1.0'])
        self.assertFalse((self.profile / 'data/packs/fixture.other/runs').exists())
        # The broken pack is quarantined and surfaced in status.
        record = background.read_state(self.profile)['verify_failures']['fixture.other']
        self.assertTrue(record['quarantined'])
        self.assertEqual(record['count'], threshold)
        self.assertIn('fixture.other', background.status(self.profile)['quarantined'])
        # Once quarantined it is no longer verified or logged each tick.
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            background.run_once(self.profile)
        self.assertNotIn('fixture.other', err.getvalue())
        self.assertEqual(background.read_state(self.profile)['verify_failures']['fixture.other']['count'],
                         threshold)
        # Disabling clears the quarantine so a later re-enable retries cleanly.
        self.store.disable('fixture.other')
        background.run_once(self.profile)
        self.assertNotIn('fixture.other', background.read_state(self.profile).get('verify_failures', {}))

    def test_verify_oserror_is_caught_not_crashing(self):
        # #139: an OSError from verify() (a missing file or a permission error,
        # not just a PackError) must be caught, not crash the scheduler.
        self.install()
        with patch.object(PackStore, 'verify', side_effect=OSError('installed code unreadable')):
            self.assertEqual(background.tasks(self.profile), [])  # no exception
            background.run_once(self.profile)                     # completes
        record = background.read_state(self.profile)['verify_failures']['fixture.command']
        self.assertEqual(record['count'], 1)
        self.assertEqual(record['error'], 'installed code unreadable')

    # Watch triggers: a resident host runs a command when watched paths change.

    def with_trigger(self, **trigger):
        declaration = self.manifest['entrypoints']['command']['commands'][0]
        declaration['trigger'] = {'watch': [str(self.root / 'watched')], 'debounce_ms': 0,
                                  'min_interval_seconds': 1, **trigger}
        declaration['schedule'] = {'timeout_seconds': 5}

    def runs(self):
        path = self.profile / 'data/packs/fixture.command/runs'
        return path.read_text().splitlines() if path.exists() else []

    def test_trigger_manifest_bounds_and_defaults(self):
        from tap_core.packs import command_schedule, TRIGGER_BACKSTOP_SECONDS
        declaration = self.manifest['entrypoints']['command']['commands'][0]
        # Without a trigger the schedule is unchanged, so existing fingerprints hold.
        self.assertIs(command_schedule(declaration), declaration['schedule'])
        bad = copy.deepcopy(self.manifest)
        del bad['entrypoints']['command']['commands'][0]['schedule']['interval_seconds']
        with self.assertRaisesRegex(PackError, 'interval_seconds'):
            validate_manifest(bad, self.source)
        self.with_trigger()
        validate_manifest(self.manifest, self.source)
        effective = command_schedule(self.manifest['entrypoints']['command']['commands'][0])
        self.assertEqual(effective['interval_seconds'], TRIGGER_BACKSTOP_SECONDS)
        self.assertEqual(effective['trigger']['min_interval_seconds'], 1)
        minimal = copy.deepcopy(self.manifest)
        minimal['entrypoints']['command']['commands'][0]['trigger'] = {'watch': ['~/.claude/projects']}
        validate_manifest(minimal, self.source)
        self.assertEqual(command_schedule(minimal['entrypoints']['command']['commands'][0])['trigger'],
                         {'watch': ['~/.claude/projects'], 'debounce_ms': 500, 'min_interval_seconds': 10})
        for change in ({'watch': []}, {'watch': ['../escape']}, {'watch': ['data/../x']},
                       {'watch': ['']}, {'watch': ['a\x00b']}, {'debounce_ms': -1},
                       {'debounce_ms': 60001}, {'min_interval_seconds': 0}, {'unknown': 1}):
            bad = copy.deepcopy(self.manifest)
            bad['entrypoints']['command']['commands'][0]['trigger'].update(change)
            with self.assertRaises(PackError, msg=change):
                validate_manifest(bad, self.source)
        orphan = copy.deepcopy(self.manifest)
        del orphan['entrypoints']['command']['commands'][0]['schedule']
        with self.assertRaisesRegex(PackError, 'requires command.schedule'):
            validate_manifest(orphan, self.source)

    def test_wake_runs_a_task_before_its_interval_and_only_that_task(self):
        self.with_trigger()
        self.install()
        background.run_once(self.profile)
        background.run_once(self.profile)
        self.assertEqual(self.runs(), ['0.1.0'])
        key = 'fixture.command:' + ' '.join(self.manifest['entrypoints']['command']['commands'][0]['path'])
        background.run_once(self.profile, wake={'other:task'})
        self.assertEqual(len(self.runs()), 1)
        active = background.run_once(self.profile, wake={key})
        self.assertEqual(len(self.runs()), 2)
        self.assertEqual([background.task_key(command) for command, _ in active], [key])
        row = background.read_state(self.profile)['jobs'][key]
        self.assertGreater(row['next_at'] - row['finished_at'], 3000)  # backstop interval

    def serve(self, factory):
        stop = threading.Event()
        thread = threading.Thread(target=background.serve, args=(self.profile, stop, factory), daemon=True)
        thread.start()
        def finish():
            stop.set()
            thread.join(10)
            self.assertFalse(thread.is_alive())
        self.addCleanup(finish)
        return stop

    def wait_for(self, predicate, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not predicate():
            time.sleep(0.02)
        return predicate()

    def test_resident_host_runs_once_per_debounced_burst_with_a_floor(self):
        self.with_trigger(debounce_ms=200, min_interval_seconds=1)
        self.install()
        callbacks, closed = [], []

        class FakeWatch:
            def __init__(self, paths, on_change):
                self.paths = paths
                callbacks.append(on_change)
            def start(self):
                return self
            def close(self):
                closed.append(self.paths)

        bound = patch.object(background, 'RESIDENT_MAX_SLEEP_SECONDS', 1)
        bound.start()
        self.addCleanup(bound.stop)
        stop = self.serve(FakeWatch)
        self.assertTrue(self.wait_for(lambda: callbacks and len(self.runs()) == 1))
        host = json.loads((self.profile / 'state/background-host.json').read_text())
        self.assertEqual(list(host['watches'].values())[0]['state'], 'watching')
        self.assertEqual(background.status(self.profile)['host']['mode'], 'resident')
        started = time.monotonic()
        for _ in range(5):  # one burst
            callbacks[-1](['changed'])
            time.sleep(0.02)
        self.assertTrue(self.wait_for(lambda: len(self.runs()) == 2))
        self.assertGreaterEqual(time.monotonic() - started, 0.2)  # debounced
        time.sleep(1.0)
        self.assertEqual(len(self.runs()), 2)  # one run per burst, no interval rerun
        callbacks[-1](['changed'])  # right after a run: held to min_interval, then runs
        self.assertTrue(self.wait_for(lambda: len(self.runs()) == 3))
        self.store.disable('fixture.command')
        self.assertTrue(self.wait_for(lambda: closed))  # pack changes picked up within the bound
        stop.set()

    def test_resident_host_polls_when_the_os_watch_is_unavailable(self):
        self.with_trigger()
        self.install()

        class Unavailable:
            def __init__(self, paths, on_change):
                pass
            def start(self):
                raise background.WatchUnavailable('no FSEvents here')

        with patch.object(background, 'WATCH_FALLBACK_SECONDS', 1), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            self.serve(Unavailable)
            self.assertTrue(self.wait_for(lambda: len(self.runs()) >= 2))
        self.assertIn('watch unavailable', err.getvalue())
        host = json.loads((self.profile / 'state/background-host.json').read_text())
        watch = list(host['watches'].values())[0]
        self.assertEqual((watch['state'], watch['error']), ('polling', 'no FSEvents here'))

    def test_resident_host_runs_on_a_real_filesystem_change(self):
        if sys.platform != 'darwin':
            self.skipTest('FSEvents is a macOS boundary')
        watched = self.root / 'watched'
        watched.mkdir()
        self.with_trigger(min_interval_seconds=1)
        self.install()
        self.serve(background.PathWatch)
        self.assertTrue(self.wait_for(lambda: len(self.runs()) == 1))
        time.sleep(1.2)
        (watched / 'session.jsonl').write_text('{}\n')
        self.assertTrue(self.wait_for(lambda: len(self.runs()) == 2))

    def test_resident_host_exits_when_core_code_changes(self):
        self.install()
        marks = iter([('a',), ('b',)])
        with patch.object(background, '_code_mark', side_effect=lambda: next(marks)), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            background.serve(self.profile, threading.Event())
        self.assertIn('core code changed', out.getvalue())

    def test_registration_is_a_resident_keepalive_agent(self):
        import plistlib
        self.install()
        adapter = Mock()
        adapter.run.return_value.returncode = 1
        plist = self.root / 'job.plist'
        with patch.object(background, 'paths', return_value=('fixture.job', plist)):
            background.reconcile(self.profile, adapter)
        value = plistlib.loads(plist.read_bytes())
        self.assertTrue(value['KeepAlive'])
        self.assertNotIn('StartInterval', value)
        self.assertEqual(value['ProgramArguments'][-1], '--resident')

    def test_coalescing_runs_an_interval_job_slightly_early_only_when_asked(self):
        self.install()
        background.run_once(self.profile)
        key = next(iter(background.read_state(self.profile)['jobs']))
        state = background.read_state(self.profile)
        state['jobs'][key]['next_at'] = time.time() + 1.5
        background.atomic_json(self.profile / 'state/background.json', state)
        background.run_once(self.profile)
        self.assertEqual(len(self.runs()), 1)
        background.run_once(self.profile, early=2)
        self.assertEqual(len(self.runs()), 2)

    def test_verify_memo_rehashes_after_any_content_change(self):
        self.install()
        background.tasks(self.profile)
        with patch('tap_core.pack_store._tree_hashes', side_effect=AssertionError('rehashed')):
            self.assertEqual(len(background.tasks(self.profile)), 1)  # unchanged tree: memo
        time.sleep(0.01)
        self.tamper('fixture.command', '0.1.0')
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(background.tasks(self.profile, account={}), [])  # drift is still caught

    def test_replacing_a_loaded_agent_waits_for_its_bootout_before_bootstrap(self):
        self.install()
        plist = self.root / 'job.plist'
        plist.write_bytes(b'old StartInterval agent')
        calls = []
        # launchd still reports the old job twice after bootout, then it is gone.
        print_results = iter([0, 0, 0, 113, 113])

        def run(argv, check=True):
            calls.append(argv[1])
            return Mock(returncode=next(print_results) if argv[1] == 'print' else 0)

        adapter = Mock(run=Mock(side_effect=run))
        with patch.object(background, 'paths', return_value=('fixture.job', plist)), \
                patch.object(background.time, 'sleep'):
            background.reconcile(self.profile, adapter)
        self.assertEqual(calls, ['print', 'bootout', 'print', 'print', 'print', 'print', 'bootstrap'])

    def test_replaced_agent_that_never_unloads_is_reported_not_left_unloaded(self):
        self.install()
        plist = self.root / 'job.plist'
        plist.write_bytes(b'old StartInterval agent')
        adapter = Mock()
        adapter.run.return_value.returncode = 0  # never unloads
        with patch.object(background, 'paths', return_value=('fixture.job', plist)), \
                patch.object(background, 'BOOTOUT_WAIT_SECONDS', 0.05), \
                patch.object(background.time, 'sleep'):
            with self.assertRaisesRegex(TapError, 'did not unload'):
                background.reconcile(self.profile, adapter)
        self.assertNotIn('bootstrap', [c.args[0][1] for c in adapter.run.call_args_list])

    def test_event_storm_during_debounce_does_not_run_a_pass_per_event(self):
        self.with_trigger(debounce_ms=1000, min_interval_seconds=1)
        self.install()
        callbacks = []

        class FakeWatch:
            def __init__(self, paths, on_change):
                callbacks.append(on_change)
            def start(self):
                return self
            def close(self):
                pass

        passes = []
        real_run_once = background.run_once

        def counting_run_once(*args, **kwargs):
            passes.append(time.monotonic())
            return real_run_once(*args, **kwargs)

        with patch.object(background, 'run_once', side_effect=counting_run_once):
            self.serve(FakeWatch)
            self.assertTrue(self.wait_for(lambda: callbacks and len(self.runs()) == 1))
            time.sleep(1.2)
            before = len(passes)
            for _ in range(50):  # a burst inside one debounce window
                callbacks[-1]([str(self.root / 'watched/file')])
                time.sleep(0.01)
            self.assertTrue(self.wait_for(lambda: len(self.runs()) == 2))
            time.sleep(0.5)
        self.assertLessEqual(len(passes) - before, 2)
        self.assertEqual(len(self.runs()), 2)

    def test_the_hosts_own_state_writes_never_trigger_a_run(self):
        self.with_trigger(debounce_ms=0, min_interval_seconds=1)
        self.install()
        callbacks = []

        class FakeWatch:
            def __init__(self, paths, on_change):
                callbacks.append(on_change)
            def start(self):
                return self
            def close(self):
                pass

        self.serve(FakeWatch)
        self.assertTrue(self.wait_for(lambda: callbacks and len(self.runs()) == 1))
        time.sleep(1.2)
        own = os.path.realpath(self.profile)
        for _ in range(20):
            callbacks[-1]([own + '/state/background.json', own + '/logs/background/abc.log'])
        time.sleep(1.5)
        self.assertEqual(len(self.runs()), 1)

    def test_a_watch_on_a_path_created_later_is_moved_onto_it(self):
        if sys.platform != 'darwin':
            self.skipTest('FSEvents is a macOS boundary')
        target = self.root / 'later' / 'sessions'
        self.with_trigger(watch=[str(target)], min_interval_seconds=1)
        self.install()
        bound = patch.object(background, 'RESIDENT_MAX_SLEEP_SECONDS', 1)
        bound.start()
        self.addCleanup(bound.stop)
        self.serve(background.PathWatch)
        host = lambda: json.loads((self.profile / 'state/background-host.json').read_text())
        self.assertTrue(self.wait_for(lambda: (self.profile / 'state/background-host.json').exists()))
        self.assertEqual(list(host()['watches'].values())[0]['roots'], [os.path.realpath(self.root)])
        target.mkdir(parents=True)
        real = os.path.realpath(target)
        self.assertTrue(self.wait_for(lambda: list(host()['watches'].values())[0]['roots'] == [real]))
        runs = len(self.runs())
        time.sleep(1.2)
        (target / 'a.jsonl').write_text('{}\n')
        self.assertTrue(self.wait_for(lambda: len(self.runs()) > runs))
