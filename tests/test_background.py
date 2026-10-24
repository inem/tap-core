import contextlib
import copy
import io
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import os
import signal
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

    def write_usage_freshness(self, observed_at):
        path = self.profile / 'data/readers/usage.meters/freshness.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'version': 1, 'observed_at': {'codex': observed_at}}))

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

    def test_freshness_coordinator_only_runs_stale_codex_adapter(self):
        self.install_usage_adapter()
        now = 1_000_000.0
        self.write_usage_freshness(now)
        commands = background.tasks(self.profile)
        with patch.object(background.time, 'time', return_value=now), \
                patch.object(background, '_run_pack') as run:
            receipts = background.run_freshness_once(self.profile, commands)
        self.assertEqual(receipts[0]['reason'], 'idle_decay')
        run.assert_not_called()

        later = now + background.freshness_policy.DEFAULT_POLICY['idle_interval'] + 1
        def collect(*_args, **_kwargs):
            self.write_usage_freshness(later)
            return 0

        with patch.object(background.time, 'time', return_value=later), \
                patch.object(background, '_run_pack', side_effect=collect) as run:
            receipts = background.run_freshness_once(self.profile, commands)
        self.assertEqual((receipts[0]['action'], receipts[0]['reason']), ('refresh', 'due_idle'))
        run.assert_called_once()
        target = next(iter(background.read_state(self.profile)['usage_freshness']['targets'].values()))
        self.assertEqual((target['last_authoritative_at'], target['failures']), (later, 0))

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

    def test_freshness_coordinator_backs_off_when_adapter_reports_no_quota(self):
        self.install_usage_adapter()
        now = 1_000_000.0
        commands = background.tasks(self.profile)
        with patch.object(background.time, 'time', return_value=now), \
                patch.object(background, '_run_pack', return_value=0) as run:
            receipts = background.run_freshness_once(self.profile, commands)
        self.assertEqual((receipts[0]['action'], receipts[0]['reason']), ('refresh', 'due_idle'))
        run.assert_called_once()
        target = next(iter(background.read_state(self.profile)['usage_freshness']['targets'].values()))
        self.assertEqual((target['failures'], target['last_error']), (1, 'error'))
        self.assertEqual(target['retry_at'], now + background.freshness_policy.DEFAULT_POLICY['backoff']['base'])

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

        def start_pack(*_args, **_kwargs):
            process = Mock()
            process.poll.side_effect = lambda: 0 if release.is_set() else None
            started.set()
            return process

        def run_background():
            try:
                background.run_once(self.profile)
            except Exception as error:
                errors.append(error)

        with patch.object(background, '_start_pack', side_effect=start_pack):
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
            # Another command of the same pack runs alongside the scheduled one.
            with command_execution_lock(self.profile, key='fixture.command', shared=True):
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

    def test_commands_share_the_lease_and_mutation_waits_for_all(self):
        with command_execution_lock(self.profile, key='pack.a', shared=True):
            with command_execution_lock(self.profile, key='pack.a', shared=True):
                pass  # two commands of one pack run concurrently
            with self.assertRaisesRegex(TapError, 'still running'):
                with command_execution_lock(self.profile, key='pack.a'):
                    pass  # mutation is refused while any command runs
        with command_execution_lock(self.profile, key='pack.a'):
            with self.assertRaisesRegex(TapError, 'still running'):
                with command_execution_lock(self.profile, key='pack.a', shared=True):
                    pass  # no command starts while the pack is being changed

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

    def recording_provider(self, delay):
        (self.source / 'command.py').write_text(
            "import os,json,pathlib,time\n"
            "c=json.loads(os.environ['TAP_COMMAND_CONTEXT'])\n"
            "p=pathlib.Path(c['output_dir'])/'events.jsonl'\n"
            "def record(phase):\n"
            " with p.open('a') as f: f.write(json.dumps(dict(phase=phase,at=time.time(),path=c['path'],pid=os.getpid()))+'\\n')\n"
            "record('start')\n"
            f"time.sleep({delay})\n"
            "record('finish')\n")

    def events(self, pack):
        path = self.profile / 'data/packs' / pack / 'events.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()]

    def wait_for(self, predicate, seconds=4):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.02)
        self.fail('timed out waiting for provider evidence')

    def host(self):
        process = subprocess.Popen([sys.executable, '-B', str(Path(background.__file__)), str(self.profile)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        def cleanup():
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
        self.addCleanup(cleanup)
        return process

    def test_short_pack_repeats_while_slow_pack_still_runs(self):
        self.manifest['entrypoints']['command']['commands'][0]['schedule'] = {
            'interval_seconds': 30, 'timeout_seconds': 5}
        self.recording_provider(2.5)
        self.install()
        self.recording_provider(0)
        self.install_other()
        # Keep production manifest bounds; shorten only this fixture's resolved
        # schedule to exercise admission during an active slow provider.
        original = background.tasks
        def tasks(*args, **kwargs):
            result = original(*args, **kwargs)
            return [(c, {**s, 'interval_seconds': 1} if c.provider_id == 'fixture.other' else s)
                    for c, s in result]
        with patch.object(background, 'tasks', side_effect=tasks), \
                patch.object(background, 'DISCOVERY_SECONDS', .05):
            background.run_once(self.profile)
        slow = self.events('fixture.command')
        fast = self.events('fixture.other')
        starts = [r['at'] for r in fast if r['phase'] == 'start']
        self.assertGreaterEqual(len(starts), 2)
        self.assertLess(starts[0], slow[-1]['at'])
        self.assertLess(starts[1], slow[-1]['at'])
        self.assertEqual(len(slow), 2)
        self.assertTrue(all(r['phase'] == 'ok' for r in background.read_state(self.profile)['jobs'].values()))

    def test_scheduled_siblings_serialize_but_other_pack_finishes_first(self):
        declaration = copy.deepcopy(self.manifest['entrypoints']['command']['commands'][0])
        declaration['path'] = ['fixture', 'second']
        self.manifest['entrypoints']['command']['commands'].append(declaration)
        self.recording_provider(.4)
        self.install()
        self.manifest['entrypoints']['command']['commands'].pop()
        # install_other reads the written manifest.
        (self.source / 'pack.json').write_text(json.dumps(self.manifest))
        self.recording_provider(0)
        self.install_other()
        background.run_once(self.profile)
        own = self.events('fixture.command')
        other = self.events('fixture.other')
        self.assertEqual([r['phase'] for r in own], ['start', 'finish', 'start', 'finish'])
        self.assertLess(other[-1]['at'], own[1]['at'])

    def test_concurrency_bound_and_overdue_queue_drain(self):
        self.recording_provider(.2)
        self.install()
        template = json.loads((self.source / 'pack.json').read_text())
        for index in range(6):
            manifest = copy.deepcopy(template)
            name = 'fixture.pack' + str(index)
            manifest['id'] = name
            manifest['entrypoints']['command']['commands'][0]['path'] = ['pack' + str(index), 'echo']
            (self.source / 'pack.json').write_text(json.dumps(manifest))
            artifact = self.root / (name + '.tap-pack')
            build_artifact(self.source, artifact)
            self.store.install(artifact)
            self.store.enable(name, '0.1.0', origins=['https://fixture.example'], capabilities=['command.execute', 'background.run'])
        with patch.object(background, 'MAX_RUNNING_PACKS', 2):
            background.run_once(self.profile)
        timeline = sorted((r['at'], 1 if r['phase'] == 'start' else -1)
                          for path in (self.profile / 'data/packs').glob('*/events.jsonl')
                          for r in map(json.loads, path.read_text().splitlines()))
        active = maximum = 0
        for _, delta in timeline:
            active += delta
            maximum = max(maximum, active)
        self.assertEqual((active, maximum), (0, 2))
        self.assertEqual(len(background.read_state(self.profile)['jobs']), 7)

    def test_timeout_does_not_delay_neighbor_and_lease_is_released(self):
        self.recording_provider(30)
        self.install()
        self.recording_provider(0)
        self.install_other()
        background.run_once(self.profile)
        jobs = background.read_state(self.profile)['jobs']
        slow, fast = jobs['fixture.command:fixture echo'], jobs['fixture.other:other echo']
        self.assertEqual((slow['phase'], slow['exit_code']), ('unknown', 124))
        self.assertEqual(fast['phase'], 'ok')
        self.assertLess(fast['finished_at'], slow['finished_at'])
        self.store.disable('fixture.command')

    def test_start_failure_isolated(self):
        self.install()
        self.install_other()
        original = background._start_pack
        def start(command, *args, **kwargs):
            if command.provider_id == 'fixture.command':
                raise OSError('fixture startup failure')
            return original(command, *args, **kwargs)
        with patch.object(background, '_start_pack', side_effect=start):
            background.run_once(self.profile)
        rows = {v['provider']: v for v in background.read_state(self.profile)['jobs'].values()}
        self.assertEqual(rows['fixture.command']['exit_code'], 125)
        self.assertEqual(rows['fixture.other']['phase'], 'ok')

    def test_sigterm_reaps_provider_and_marks_unknown(self):
        self.manifest['entrypoints']['command']['commands'][0]['schedule']['timeout_seconds'] = 30
        self.recording_provider(30)
        self.install()
        process = self.host()
        evidence = self.profile / 'data/packs/fixture.command/events.jsonl'
        self.wait_for(evidence.exists)
        pid = self.events('fixture.command')[0]['pid']
        process.terminate()
        process.wait(timeout=5)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        row = next(iter(background.read_state(self.profile)['jobs'].values()))
        self.assertEqual((row['phase'], row['exit_code']), ('unknown', 130))
        self.store.disable('fixture.command')

    def test_replacement_host_does_not_overlap_orphan_provider(self):
        self.manifest['entrypoints']['command']['commands'][0]['schedule']['timeout_seconds'] = 30
        self.recording_provider(30)
        self.install()
        process = self.host()
        evidence = self.profile / 'data/packs/fixture.command/events.jsonl'
        self.wait_for(evidence.exists)
        pid = self.events('fixture.command')[0]['pid']
        self.addCleanup(lambda: self.kill_orphan(pid))
        process.kill()
        process.wait(timeout=5)
        # Even after the old nominal due time, its inherited scheduler lease
        # prevents an overlapping writer. No live profile is used here.
        state = background.read_state(self.profile)
        next(iter(state['jobs'].values()))['next_at'] = 0
        background.atomic_json(self.profile / 'state/background.json', state)
        background.run_once(self.profile)
        self.assertEqual(len(self.events('fixture.command')), 1)
        with self.assertRaises(TapError):
            with command_execution_lock(self.profile, key='fixture.command'):
                pass

    @staticmethod
    def kill_orphan(pid):
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def test_each_scheduled_run_leaves_started_and_finished_receipts(self):
        self.install()
        background.run_once(self.profile)
        row = next(iter(background.read_state(self.profile)['jobs'].values()))
        receipt = Path(row['receipt_dir'])
        self.assertEqual(receipt.parent, (self.profile / 'state/background-runs').resolve())
        started = json.loads((receipt / 'started.json').read_text())
        finished = json.loads((receipt / 'finished.json').read_text())
        self.assertEqual(started['schema'], 'tap.background-run/v1')
        self.assertEqual((started['trigger']['kind'], started['run']), ('schedule', row['run']))
        self.assertEqual(len(started['implementation']['manifest_sha256']), 64)
        self.assertEqual((finished['exit_code'], finished['phase']), (0, 'ok'))
