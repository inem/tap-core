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
                with stream.open('ab') as handle:
                    handle.write(encoded(capture_record(body='live-one')))
                output = root / 'data/readers' / PACK / 'events.jsonl'
                self.assertTrue(until(lambda: output.exists() and len(output.read_text().splitlines()) == 1))
                self.assertEqual(state()['pid'], original_pid)
                store.disable(PACK, live=True)
                os.kill(process.pid, signal.SIGUSR1)
                self.assertTrue(until(lambda: PACK not in state().get('readers', {})))
                count = len(output.read_text().splitlines())
                with stream.open('ab') as handle:
                    handle.write(encoded(capture_record(body='live-two')))
                time.sleep(0.7)
                self.assertEqual(len(output.read_text().splitlines()), count)
                # Reattaching the same reader must skip events seen while detached.
                store.enable(PACK, '0.1.0', origins=[ORIGIN],
                             capabilities=['capture.read'], live=True)
                os.kill(process.pid, signal.SIGUSR1)
                self.assertTrue(until(lambda: PACK in state().get('readers', {})))
                with stream.open('ab') as handle:
                    handle.write(encoded(capture_record(body='live-three')))
                self.assertTrue(until(lambda: len(output.read_text().splitlines()) == 2))
                events = [json.loads(line)['body'] for line in output.read_text().splitlines()]
                self.assertEqual(events, ['live-one', 'live-three'])
                self.assertEqual(state()['pid'], original_pid)
            finally:
                process.terminate()
                process.communicate(timeout=8)

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


class ReaderIdleIoTests(unittest.TestCase):
    """A running controller leaves idle reader state untouched (#256)."""

    def test_idle_controller_does_not_rewrite_or_chmod_reader_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'pack-source'
            source.mkdir()
            (source / 'reader.py').write_text(
                "import json, os, pathlib, sys\n"
                "event = json.loads(sys.stdin.readline())\n"
                "context = json.loads(os.environ['TAP_PACK_CONTEXT'])\n"
                "target = pathlib.Path(context['output_dir']) / (context['reader_lane'] + '.jsonl')\n"
                "with target.open('a') as out: out.write(json.dumps(event['body']) + '\\n')\n"
            )
            (source / 'pack.json').write_text(json.dumps({
                'manifest_version': 1, 'id': PACK, 'version': '0.1.0',
                'requires': {'pack_api': 1, 'dependencies': []},
                'files': ['reader.py'],
                'entrypoints': {'reader': {'file': 'reader.py', 'interface': 'python-jsonl-v1',
                                           'delivery': 'fresh-and-replay-v1'}},
                'config': {},
                'access': {'origins': [ORIGIN], 'capabilities': ['capture.read']},
            }))
            artifact = root / 'reader.tap-pack'
            build_artifact(source, artifact)
            profile = Profile(root, '/fixture/backend', 19000, 'explicit',
                              'http://fixture.test', [],
                              bridge=dict(bridge_config(), enabled=False),
                              components={'version': 1, 'python': sys.executable,
                                          'bun': '/usr/bin/true', 'readers': {}, 'handlers': {}})
            profile.save()
            store = PackStore(root)
            store.install(artifact)
            store.enable(PACK, '0.1.0', origins=[ORIGIN], capabilities=['capture.read'])
            # Retained history for the replay lane: unrelated traffic, then one match.
            unrelated = []
            for index in range(120):
                record = capture_record(body=f'unrelated-{index}')
                record['url'] = 'https://unrelated.example/data'
                unrelated.append(record)
            stream = root / 'data/stream.jsonl'
            stream.parent.mkdir(parents=True, exist_ok=True)
            stream.write_bytes(b''.join(encoded(record) for record in unrelated)
                               + encoded(capture_record(body='retained-match')))
            process = subprocess.Popen(
                [sys.executable, '-B', '-m', 'tap_core.service', str(root)],
                cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            state_dir = root / 'state/readers' / PACK
            output = root / 'data/readers' / PACK
            watched = [state_dir, state_dir / 'work', output, root / 'logs/readers' / PACK,
                       state_dir / 'checkpoint.json', state_dir / 'fresh-checkpoint.json']

            def processed(lane):
                try:
                    name = 'fresh-checkpoint.json' if lane == 'fresh' else 'checkpoint.json'
                    return json.loads((state_dir / name).read_text())['processed']
                except (OSError, ValueError):
                    return -1

            def snapshot():
                # A replace changes the inode; a chmod or entry change moves ctime.
                return {str(path): (path.stat().st_ino, path.stat().st_ctime_ns) for path in watched}

            try:
                # 121 records at one per 0.25 s tick took ~30 s; batched skips need a few ticks.
                self.assertTrue(until(lambda: processed('replay') == 121, timeout=8),
                                f'replay lane processed {processed("replay")} of 121')
                self.assertEqual((output / 'replay.jsonl').read_text().splitlines(), ['"retained-match"'])
                self.assertTrue(until(lambda: processed('fresh') == 0))
                time.sleep(2)  # let the last run record its idle phase
                before = snapshot()
                time.sleep(4)  # several idle ticks of both lanes
                self.assertEqual(snapshot(), before)
                with stream.open('ab') as handle:
                    handle.write(encoded(capture_record(body='live-match')))
                self.assertTrue(until(lambda: (output / 'fresh.jsonl').exists()))
                self.assertEqual((output / 'fresh.jsonl').read_text().splitlines(), ['"live-match"'])
            finally:
                process.terminate()
                process.communicate(timeout=8)
