"""Managed reader-only on/off without Hub/Bun (#14 hubless slice)."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, PropertyMock, patch

from tap_core.components import Job, _start, configuration, needs_hub, status, stop
from tap_core.pack_store import PackStore, build_artifact
from tap_core.readers import Reader
from tap_core.runtime import Profile, TapError
from test_bridge import config
from test_records_journal import capture_record, encoded

ROOT = Path(__file__).resolve().parents[1]


class HublessReaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='tap-hubless-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.profile_root = self.root / 'profile'
        self.bridge = config(enabled=False, hub_port=19301, allow_origins=[], exclude_origins=[],
                             page_scripts=[])
        # Absolute bun path that does not need to exist for reader-only.
        self.components = dict(version=1, python=sys.executable, bun='/hubless/unused-bun',
                               readers={}, handlers={})
        self.profile = Profile(self.profile_root, '/fixture/backend', 19302, 'explicit',
                               'http://fixture.example', [], bridge=self.bridge,
                               components=self.components)
        self.profile.save()
        self.store = PackStore(self.profile_root)
        self.controller = None

    def tearDown(self):
        if self.controller is not None and self.controller.poll() is None:
            try:
                os.killpg(self.controller.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                self.controller.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(self.controller.pid, signal.SIGKILL)
                self.controller.wait(timeout=3)
        if self.controller is not None and self.controller.stdout is not None:
            self.controller.stdout.close()
        self.controller = None

    def install_reader(self):
        source = self.root / 'reader-src'
        shutil.copytree(ROOT / 'fixtures/packs/reader', source)
        artifact = self.root / 'reader.tap-pack'
        build_artifact(source, artifact)
        self.store.install(artifact)
        self.store.enable('example.reader', '0.1.0',
                          origins=['https://fixture.example'], capabilities=['capture.read'])

    def seed_journal(self, *bodies):
        stream = self.profile_root / 'data/stream.jsonl'
        stream.parent.mkdir(parents=True, exist_ok=True)
        stream.write_bytes(b''.join(encoded(capture_record(body=body)) for body in bodies))

    def append_journal(self, body):
        stream = self.profile_root / 'data/stream.jsonl'
        stream.parent.mkdir(parents=True, exist_ok=True)
        with stream.open('ab') as handle:
            handle.write(encoded(capture_record(body=body)))

    def spawn_controller(self, args, **kwargs):
        if args[:2] == ['/bin/launchctl', 'bootstrap']:
            self.controller = subprocess.Popen(
                [sys.executable, '-B', str(ROOT / 'tap_core/service.py'), str(self.profile_root)],
                start_new_session=True,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            return subprocess.CompletedProcess(args, 0, stdout='', stderr='')
        if args[:2] == [sys.executable, '-c']:
            return subprocess.CompletedProcess(args, 0, stdout='', stderr='')
        if len(args) == 2 and args[1] == '--version' and args[0] == self.components['bun']:
            return subprocess.CompletedProcess(args, 0, stdout='1.3.11\n', stderr='')
        return subprocess.CompletedProcess(args, 0, stdout='', stderr='')

    def start_hubless(self):
        adapter = Mock()
        adapter.port_open.return_value = False
        adapter.service_loaded.return_value = False
        adapter.run.side_effect = self.spawn_controller
        adapter.service_pid.side_effect = (
            lambda job: self.controller.pid if self.controller is not None and self.controller.poll() is None else None)

        def wait_healthy(predicate, seconds=15):
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                if predicate():
                    return True
                time.sleep(0.05)
            return predicate()

        adapter.wait.side_effect = wait_healthy
        plist = self.root / 'components.plist'
        with patch.object(Job, 'plist', new_callable=PropertyMock, return_value=plist):
            _start(Profile.load(self.profile_root), adapter)
        return adapter

    def test_configuration_allows_reader_only_without_enabled_bridge(self):
        self.assertFalse(needs_hub(self.components, self.bridge))
        configuration(self.components, self.profile)
        with self.assertRaisesRegex(TapError, 'Handlers require an enabled bridge'):
            configuration(dict(self.components, handlers={
                'echo': {'command': [sys.executable, '-c', 'pass'], 'config': {},
                         'origins': ['https://fixture.example']}}), self.profile)

    def test_enabled_page_bridge_requires_hub_without_handlers(self):
        bridge = config(enabled=True, hub_port=19311, allow_origins=[], exclude_origins=[],
                        page_scripts=[])
        components = dict(version=1, python=sys.executable, bun='/usr/bin/true',
                          readers={}, handlers={})
        profile = Profile(self.root / 'hubful', '/fixture/backend', 19312, 'explicit',
                          'http://fixture.example', [], bridge=bridge, components=components)
        self.assertTrue(needs_hub(components, bridge))
        configuration(components, profile)
        with self.assertRaisesRegex(TapError, 'absolute path when Hub is required'):
            configuration(dict(components, bun='relative-bun'), profile)

    def test_enabled_page_bridge_starts_hub_without_handlers(self):
        import socket
        bun = os.environ.get('TAP_TEST_BUN') or shutil.which('bun')
        if not bun:
            self.skipTest('Set TAP_TEST_BUN for the mandatory development Hub')
        with socket.socket() as reservation:
            reservation.bind(('127.0.0.1', 0))
            hub_port = reservation.getsockname()[1]
        self.bridge = config(enabled=True, hub_port=hub_port,
                             allow_origins=['https://fixture.example'],
                             exclude_origins=[], page_scripts=[])
        self.components['bun'] = bun
        self.profile.bridge = self.bridge
        self.profile.components = self.components
        self.profile.save()
        adapter = self.start_hubless()
        observed = status(Profile.load(self.profile_root), adapter)
        self.assertTrue(observed['ready'], observed)
        self.assertIsInstance(observed.get('hub_pid'), int)

    def test_components_without_bridge_rejected_before_save(self):
        components = dict(version=1, python=sys.executable, bun='/hubless/unused-bun',
                          readers={}, handlers={})
        with self.assertRaisesRegex(TapError, 'require a bridge configuration'):
            Profile(self.root / 'no-bridge', '/fixture/backend', 19313, 'explicit',
                    'http://fixture.example', [], bridge=None, components=components)

    def test_updater_busy_detects_hubless_components_controller(self):
        spec = importlib.util.spec_from_file_location('hubless_ownership', ROOT / 'instll/ownership.py')
        own = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(own)
        self.install_reader()
        profile = Profile.load(self.profile_root)
        adapter = Mock()
        adapter.service_loaded.side_effect = (
            lambda target: isinstance(target, Job) or getattr(target, 'label', '').endswith('.components'))
        adapter.port_open.return_value = False
        busy = own._profile_processes_busy(adapter, profile)
        self.assertIn('components service', busy)
        self.assertNotIn('hub port', busy)
        # Enabling the browser/control plane makes its Hub port owned infrastructure.
        profile.bridge['enabled'] = True
        profile.save()
        adapter.port_open.side_effect = lambda target: isinstance(target, Job)
        busy = own._profile_processes_busy(adapter, Profile.load(self.profile_root))
        self.assertIn('components service', busy)
        self.assertIn('hub port', busy)

    def test_hubless_on_off_preserves_checkpoint_without_bun(self):
        self.install_reader()
        projected = self.store.effective_components(self.components)
        self.assertIn('example.reader', projected['readers'])
        self.assertEqual(projected['handlers'], {})
        self.assertFalse(needs_hub(projected, self.bridge))
        self.seed_journal('{"item":1}')
        adapter = self.start_hubless()
        observed = status(Profile.load(self.profile_root), adapter)
        self.assertTrue(observed['ready'], observed)
        self.assertIsNone(observed.get('hub_pid'))
        self.assertIn('example.reader', observed['readers'])
        deadline = time.monotonic() + 10
        checkpoint = self.profile_root / 'state/readers/example.reader/checkpoint.json'
        while time.monotonic() < deadline:
            if checkpoint.is_file():
                progress = json.loads(checkpoint.read_text())
                if progress.get('processed', 0) >= 1:
                    break
            time.sleep(0.05)
        else:
            self.fail('reader did not advance checkpoint: ' + (checkpoint.read_text() if checkpoint.exists() else 'missing'))
        first = json.loads(checkpoint.read_text())
        self.assertGreaterEqual(first['processed'], 1)
        output = self.profile_root / 'data/readers/example.reader/observations.jsonl'
        self.assertTrue(output.is_file())
        self.assertIn('"item": 1', output.read_text())

        adapter.service_loaded.return_value = True
        adapter.wait.reset_mock()
        stop(Profile.load(self.profile_root), adapter)
        if self.controller is not None and self.controller.poll() is None:
            os.killpg(self.controller.pid, signal.SIGTERM)
            self.controller.wait(timeout=5)
        if self.controller is not None and self.controller.stdout is not None:
            self.controller.stdout.close()
        self.controller = None
        adapter.stop.assert_called()
        adapter.wait.assert_not_called()

        retained = json.loads(checkpoint.read_text())
        self.assertEqual(retained['processed'], first['processed'])
        self.assertEqual(retained['cursor'], first['cursor'])

        self.append_journal('{"item":2}')
        adapter = self.start_hubless()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            progress = json.loads(checkpoint.read_text())
            if progress.get('processed', 0) >= first['processed'] + 1:
                break
            time.sleep(0.05)
        else:
            self.fail('resume did not process new record')
        resumed = json.loads(checkpoint.read_text())
        self.assertGreater(resumed['processed'], first['processed'])
        self.assertEqual(resumed['definition'], first['definition'])
        self.assertIn('"item": 2', output.read_text())
        stop(Profile.load(self.profile_root), adapter)

    def test_retention_gap_is_skipped_and_reader_keeps_processing(self):
        self.install_reader()
        self.seed_journal('{"item":"retained-before-gap"}')
        profile = Profile.load(self.profile_root)
        spec = self.store.effective_components(self.components)['readers']['example.reader']
        Reader(profile, 'example.reader').run(spec, max_records=1, timeout=5)

        # Replace the only segment after the acknowledged cursor. The reader
        # records that loss and continues from the earliest retained record.
        self.seed_journal('{"item":"current-after-gap"}')
        adapter = self.start_hubless()
        observed = status(Profile.load(self.profile_root), adapter)
        self.assertTrue(observed['ready'], observed)

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            observed = status(Profile.load(self.profile_root), adapter)
            reader = observed.get('readers', {}).get('example.reader', {})
            if reader.get('phase') == 'waiting':
                break
            time.sleep(0.05)
        else:
            self.fail('reader did not continue after the retention gap: ' + repr(observed))

        self.assertTrue(observed['ready'], observed)
        self.assertTrue(observed['healthy'], observed)
        self.assertTrue(observed['workloads_healthy'], observed)
        self.assertIsNone(reader['error'])
        checkpoint = Reader(profile, 'example.reader').load()
        self.assertEqual(checkpoint['processed'], 2)
        self.assertEqual(checkpoint['phase'], 'idle')
        receipt = json.loads(Reader(profile, 'example.reader').last_gap.read_text())
        self.assertEqual(receipt['recovery'], 'earliest-retained')
        stop(Profile.load(self.profile_root), adapter)
