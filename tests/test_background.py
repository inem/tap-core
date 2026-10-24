import contextlib
import copy
import io
import json
from pathlib import Path
import re
import shutil
import tempfile
import threading
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

    def install_usage_adapter(self, version='0.22.0'):
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
        observed = {p: observed_at for p in ('codex', 'antigravity', 'cursor', 'factory', 'openrouter')}
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

    def test_freshness_coordinator_only_runs_selected_due_target(self):
        self.install_usage_adapter()
        now = 1_000_000.0
        self.write_usage_freshness(now)
        commands = background.tasks(self.profile)
        with patch.object(background.time, 'time', return_value=now), patch.object(background, '_run_pack') as run:
            receipts = background.run_freshness_once(self.profile, commands)
        self.assertEqual(len(receipts), 5)
        run.assert_not_called()
        later = now + background.freshness_policy.DEFAULT_POLICY['idle_interval'] + 1
        def collect(command, root, argv, **kwargs):
            self.assertEqual(argv, ['antigravity', '--max-age', '19800'])
            self.write_usage_freshness(now, antigravity=later)
            return 0
        with patch.object(background.time, 'time', return_value=later), patch.object(background, '_run_pack', side_effect=collect) as run:
            receipts = background.run_freshness_once(self.profile, commands)
        run.assert_called_once()
        self.assertEqual(sum(r['action'] == 'refresh' for r in receipts), 1)
        targets = self.freshness_targets()
        for key, row in targets.items():
            selected = row['provider'] == 'antigravity'
            self.assertEqual(row['last_authoritative_at'], later if selected else now)
            self.assertEqual(row['attempts'], [later] if selected else [])
        self.assertEqual([(r['target']['provider'], r['ride_along']) for r in self.execution_receipts()], [('antigravity', False)])

    def test_antigravity_activity_does_not_pull_other_clients_along(self):
        self.install_usage_adapter()
        now = 1_000_000.0
        self.write_usage_freshness(now - 1000)
        self.write_usage_activity([{'schema': 2, 'record': 'activity', 'client': 'antigravity-hub', 'observed_at': now - 10}])
        def collect(command, root, argv, **kwargs):
            self.assertEqual(argv[0], 'antigravity')
            self.write_usage_freshness(now - 1000, antigravity=now)
            return 0
        with patch.object(background.time, 'time', return_value=now), patch.object(background, '_run_pack', side_effect=collect) as run:
            receipts = background.run_freshness_once(self.profile, background.tasks(self.profile))
        by = {r['target']['provider']: r for r in receipts}
        self.assertEqual((by['antigravity']['tier'], by['antigravity']['action']), ('active', 'refresh'))
        run.assert_called_once()
        self.assertEqual(self.freshness_targets()['usage.meters:usage collect/codex']['last_authoritative_at'], now - 1000)

    def test_activity_the_snapshot_already_covers_keeps_antigravity_idle(self):
        self.install_usage_adapter()
        now = 1_000_000.0
        self.write_usage_freshness(now - 1000)
        self.write_usage_activity([{'schema': 2, 'record': 'activity', 'client': 'antigravity-hub', 'observed_at': now - 2000}])
        with patch.object(background.time, 'time', return_value=now), patch.object(background, '_run_pack') as run:
            receipts = background.run_freshness_once(self.profile, background.tasks(self.profile))
        self.assertEqual({r['tier'] for r in receipts}, {'idle'})
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
                         {'codex': 1, 'antigravity': 0, 'cursor': 0, 'factory': 0, 'openrouter': 0})

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

    def test_failure_backs_off_only_selected_client(self):
        self.install_usage_adapter()
        now = 1_000_000.0
        with patch.object(background.time, 'time', return_value=now), patch.object(background, '_run_pack', return_value=0):
            receipts = background.run_freshness_once(self.profile, background.tasks(self.profile))
        selected = next(r['target']['provider'] for r in receipts if r['action'] == 'refresh')
        for target in self.freshness_targets().values():
            self.assertEqual(target['failures'], 1 if target['provider'] == selected else 0)
            self.assertEqual(target['attempts'], [now] if target['provider'] == selected else [])
        self.assertEqual(len(self.execution_receipts()), 1)

    def test_another_clients_success_does_not_clear_backoff(self):
        self.install_usage_adapter()
        now = 1_000_000.0
        self.write_usage_freshness(now, codex=now - 20000)
        state = background.read_state(self.profile)
        stuck = background.freshness_policy.new_state('antigravity', adapter='usage.meters:usage collect')
        stuck.update(failures=3, retry_at=now + 3600, last_error='timeout')
        state['usage_freshness'] = {'version': 1, 'next_wake_at': 0, 'targets': {'usage.meters:usage collect/antigravity': stuck}}
        background.atomic_json(self.profile / 'state/background.json', state)
        def collect(command, root, argv, **kwargs):
            self.assertEqual(argv[0], 'codex')
            self.write_usage_freshness(now)
            return 0
        with patch.object(background.time, 'time', return_value=now), patch.object(background, '_run_pack', side_effect=collect):
            background.run_freshness_once(self.profile, background.tasks(self.profile))
        anti = self.freshness_targets()['usage.meters:usage collect/antigravity']
        self.assertEqual((anti['failures'], anti['retry_at'], anti['attempts']), (3, now + 3600, []))

    def write_passive_quota(self, client, ts, until=None, account=None):
        path = self.profile / 'data/readers/usage.meters/capture-freshness.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            data = json.loads(path.read_text())
        except FileNotFoundError:
            data = {'schema': 'tap.usage-passive-quota/v1', 'clients': {}}
        data['clients'][client] = {account or '': {'account': account, 'observed_at': ts, 'valid_until': until}}
        path.write_text(json.dumps(data))

    def test_fresh_capture_prevents_network_for_each_client(self):
        self.install_usage_adapter()
        now = 1_000_000.0
        self.write_usage_freshness(now - 30000)
        for client in ('codex', 'antigravity', 'cursor', 'factory', 'openrouter'):
            self.write_passive_quota(client, now - 10)
        with patch.object(background.time, 'time', return_value=now), patch.object(background, '_run_pack') as run:
            receipts = background.run_freshness_once(self.profile, background.tasks(self.profile))
        run.assert_not_called()
        self.assertTrue(all(r['action'] == 'skip' for r in receipts))
        self.assertTrue(all(t['last_authoritative_via'] == 'passive-capture' for t in self.freshness_targets().values()))
        self.assertTrue(all(not t['attempts'] for t in self.freshness_targets().values()))

    def test_capture_expiry_remains_due_across_wakes(self):
        self.install_usage_adapter()
        now = 1_000_000.0
        self.write_usage_freshness(now - 30000)
        for client in ('codex', 'antigravity', 'cursor', 'factory', 'openrouter'):
            self.write_passive_quota(client, now - 10, now + 120)
        with patch.object(background.time, 'time', return_value=now), patch.object(background, '_run_pack') as run:
            background.run_freshness_once(self.profile, background.tasks(self.profile))
        run.assert_not_called()
        with patch.object(background.time, 'time', return_value=now + 121), patch.object(background, '_run_pack', return_value=0) as run:
            receipts = background.run_freshness_once(self.profile, background.tasks(self.profile))
        run.assert_called_once()
        selected = next(r for r in receipts if r['action'] == 'refresh')
        self.assertEqual(selected['reason'], 'quota_expired')

    def test_capture_arriving_during_dispatch_settles_as_passive(self):
        self.install_usage_adapter()
        now = 1_000_000.0
        self.write_usage_freshness(now, cursor=now - 30000)
        def collect(command, root, argv, **kwargs):
            self.assertEqual(argv[0], 'cursor')
            # The pack's own pre-request check returns without contacting API.
            self.write_passive_quota('cursor', now)
            return 0
        with patch.object(background.time, 'time', return_value=now), patch.object(background, '_run_pack', side_effect=collect):
            background.run_freshness_once(self.profile, background.tasks(self.profile))
        row = self.freshness_targets()['usage.meters:usage collect/cursor']
        self.assertEqual((row['failures'], row['last_authoritative_via']), (0, 'passive-capture'))
        self.assertEqual(self.execution_receipts()[0]['outcome'], 'passive_fresh')

    def test_upgrade_does_not_wait_for_old_batch_deadline(self):
        self.install_usage_adapter()
        now = 1_000_000.0
        self.write_usage_freshness(now)
        state = background.read_state(self.profile)
        state['usage_freshness'] = {'version': 1, 'next_wake_at': now + 900, 'targets': {}}
        background.atomic_json(self.profile / 'state/background.json', state)
        with patch.object(background.time, 'time', return_value=now), patch.object(background, '_run_pack') as run:
            receipts = background.run_freshness_once(self.profile, background.tasks(self.profile))
        self.assertEqual(len(receipts), 5)
        run.assert_not_called()
        account = background.read_state(self.profile)['usage_freshness']
        self.assertEqual(account['next_wake_at'], now + 60)
        self.assertEqual(account['selector_version'], 1)

    def test_legacy_pack_is_never_called_with_ignored_selector(self):
        self.install_usage_adapter(version='0.21.0')
        with patch.object(background, '_run_pack') as run:
            background.run_freshness_once(self.profile, background.tasks(self.profile))
            background.run_once(self.profile)
        run.assert_not_called()

    def test_bad_future_expired_or_wrong_account_capture_is_ignored(self):
        spec = background.FRESHNESS_ADAPTERS[('usage.meters', ('usage', 'collect'))]['targets'][0]
        now = 1_000_000.0
        for ts, until in ((float('nan'), None), (now + 1000, None), (now, now - 1)):
            self.write_passive_quota('codex', ts, until)
            self.assertIsNone(background._passive_at(self.profile, spec, now))
        self.write_passive_quota('codex', now, account='other')
        live = self.profile / 'data/readers/usage.meters/freshness.json'
        live.write_text(json.dumps({'version': 1, 'observed_at': {}, 'accounts': {'codex': 'current'}}))
        self.assertIsNone(background._passive_at(self.profile, spec, now))

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
