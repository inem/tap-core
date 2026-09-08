"""Managed reader-only on/off without Hub/Bun (#14 hubless slice)."""
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
        self.assertFalse(needs_hub(self.components))
        configuration(self.components, self.profile)
        with self.assertRaisesRegex(TapError, 'Handlers require an enabled bridge'):
            configuration(dict(self.components, handlers={
                'echo': {'command': [sys.executable, '-c', 'pass'], 'config': {},
                         'origins': ['https://fixture.example']}}), self.profile)

    def test_hubless_on_off_preserves_checkpoint_without_bun(self):
        self.install_reader()
        projected = self.store.effective_components(self.components)
        self.assertIn('example.reader', projected['readers'])
        self.assertEqual(projected['handlers'], {})
        self.assertFalse(needs_hub(projected))
        self.seed_journal('{"item":1}')
        adapter = self.start_hubless()
        observed = status(Profile.load(self.profile_root), adapter)
        self.assertTrue(observed['healthy'], observed)
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
