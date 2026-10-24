"""An installed reader joins and leaves a running controller without restarting it."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from tap_core.pack_store import PackStore, build_artifact
from tap_core.runtime import Profile
from test_bridge import config as bridge_config
from test_records_journal import capture_record, encoded
from tap_core.routes import RouteTable


def append(root, record):
    """Append like the capture writer: the record first, then route notifications."""
    with (root / 'data/stream.jsonl').open('ab') as handle:
        handle.write(encoded(record))
    table = RouteTable(root / 'state')
    table.notify(record)
    table.close()


ROOT = Path(__file__).resolve().parents[1]
PACK = 'fixture.live-reader'
ORIGIN = 'https://fixture.example'


def until(predicate, timeout=8):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return bool(predicate())


class LiveReaderReconcileTests(unittest.TestCase):
    def test_attach_detach_keeps_controller_pid_and_cursor(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'pack-source'
            source.mkdir()
            (source / 'reader.py').write_text(
                "import json, os, pathlib, sys\n"
                "event = json.loads(sys.stdin.readline())\n"
                "context = json.loads(os.environ['TAP_PACK_CONTEXT'])\n"
                "target = pathlib.Path(context['output_dir']) / 'events.jsonl'\n"
                "with target.open('a') as out: out.write(json.dumps(event) + '\\n')\n"
            )
            (source / 'pack.json').write_text(json.dumps({
                'manifest_version': 1, 'id': PACK, 'version': '0.1.0',
                'requires': {'pack_api': 1, 'dependencies': []},
                'files': ['reader.py'],
                'entrypoints': {'reader': {'file': 'reader.py', 'interface': 'python-jsonl-v1'}},
                'config': {},
                'access': {'origins': [ORIGIN], 'capabilities': ['capture.read']},
            }))
            artifact = root / 'reader.tap-pack'
            build_artifact(source, artifact)
            bridge = dict(bridge_config(), enabled=False)
            profile = Profile(root, '/fixture/backend', 19000, 'explicit',
                              'http://fixture.test', [], bridge=bridge,
                              components={'version': 1, 'python': sys.executable,
                                          'bun': '/usr/bin/true', 'readers': {}, 'handlers': {}})
            profile.save()
            store = PackStore(root)
            store.install(artifact)
            # Already retained before this subscriber exists: must not be delivered.
            stream = root / 'data/stream.jsonl'
            stream.write_bytes(encoded(capture_record(body='old-before-attach')))
            process = subprocess.Popen(
                [sys.executable, '-B', '-m', 'tap_core.service', str(root)],
                cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            def state():
                try:
                    return json.loads((root / 'state/components.json').read_text())
                except (OSError, ValueError):
                    return {}
            try:
                self.assertTrue(until(lambda: state().get('phase') == 'ready'))
                original_pid = state()['pid']
                store.enable(PACK, '0.1.0', origins=[ORIGIN],
                             capabilities=['capture.read'], live=True)
                os.kill(process.pid, signal.SIGUSR1)
                self.assertTrue(until(lambda: PACK in state().get('readers', {})))
                append(root, capture_record(body='live-one'))
                output = root / 'data/readers' / PACK / 'events.jsonl'
                self.assertTrue(until(lambda: output.exists() and len(output.read_text().splitlines()) == 1))
                self.assertEqual(state()['pid'], original_pid)
                store.disable(PACK, live=True)
                os.kill(process.pid, signal.SIGUSR1)
                self.assertTrue(until(lambda: PACK not in state().get('readers', {})))
                count = len(output.read_text().splitlines())
                append(root, capture_record(body='live-two'))
                time.sleep(0.7)
                self.assertEqual(len(output.read_text().splitlines()), count)
                # Reattaching the same reader must skip events seen while detached.
                store.enable(PACK, '0.1.0', origins=[ORIGIN],
                             capabilities=['capture.read'], live=True)
                os.kill(process.pid, signal.SIGUSR1)
                self.assertTrue(until(lambda: PACK in state().get('readers', {})))
                append(root, capture_record(body='live-three'))
                self.assertTrue(until(lambda: len(output.read_text().splitlines()) == 2))
                events = [json.loads(line)['body'] for line in output.read_text().splitlines()]
                self.assertEqual(events, ['live-one', 'live-three'])
                self.assertEqual(state()['pid'], original_pid)
            finally:
                process.terminate()
                process.communicate(timeout=8)

    @unittest.expectedFailure  # live: PackStore.verify / resource checks heal hash drift instead of failing (40e6b21); decision in #275
    def test_failed_refresh_retries_without_another_signal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact = root / 'reader.tap-pack'
            build_artifact(ROOT / 'fixtures/packs/reader', artifact)
            profile = Profile(root, '/fixture/backend', 19000, 'explicit',
                              'http://fixture.test', [],
                              bridge=dict(bridge_config(), enabled=False),
                              components={'version': 1, 'python': sys.executable,
                                          'bun': '/usr/bin/true', 'readers': {}, 'handlers': {}})
            profile.save()
            store = PackStore(root)
            store.install(artifact)
            process = subprocess.Popen(
                [sys.executable, '-B', '-m', 'tap_core.service', str(root)],
                cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

            def state():
                try:
                    return json.loads((root / 'state/components.json').read_text())
                except (OSError, ValueError):
                    return {}

            try:
                self.assertTrue(until(lambda: state().get('phase') == 'ready'))
                pid = state()['pid']
                name = 'example.reader'
                store.enable(name, '0.1.0', origins=[ORIGIN],
                             capabilities=['capture.read'], live=True)
                installed = store.version_root(name, '0.1.0') / 'reader.py'
                original = installed.read_bytes()
                installed.write_bytes(original + b'\n# deliberately invalidates hash\n')
                os.kill(process.pid, signal.SIGUSR1)
                self.assertTrue(until(lambda: 'reconciliation pending' in
                                      (state().get('error') or '')),
                                'failed refresh must be exposed in controller health')
                self.assertNotIn(name, state().get('readers', {}))
                installed.write_bytes(original)
                # No second signal, CLI command or restart: controller retries.
                self.assertTrue(until(lambda: name in state().get('readers', {}) and
                                      state().get('error') is None, timeout=12),
                                'controller did not reconcile after repairing pack bytes')
                self.assertEqual(state()['pid'], pid)
                stream = root / 'data/stream.jsonl'
                stream.write_bytes(encoded(capture_record(body='{"marker":"after-repair"}')))
                self.assertTrue(until(lambda: (state().get('readers', {}).get(name, {})
                                                .get('progress') or {}).get('processed', 0) >= 1))
                self.assertEqual(state()['pid'], pid)
            finally:
                process.terminate()
                process.communicate(timeout=8)
