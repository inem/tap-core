"""A running controller's readers sleep while the stream is idle and wake on growth."""
import json
import os
from pathlib import Path
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
PACK = 'fixture.wake-reader'
ORIGIN = 'https://fixture.example'


def until(predicate, timeout=8):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return bool(predicate())


def record(path, body):
    value = capture_record(body=body)
    value['url'] = ORIGIN + path
    return encoded(value)


@unittest.skipUnless(sys.platform == 'darwin', 'stream growth uses kqueue')
class ReaderWakeTests(unittest.TestCase):
    def make_profile(self, root, delivery=None):
        source = root / 'pack-source'
        source.mkdir()
        (source / 'reader.py').write_text(
            "import json, os, pathlib, sys\n"
            "event = json.loads(sys.stdin.readline())\n"
            "context = json.loads(os.environ['TAP_PACK_CONTEXT'])\n"
            "target = pathlib.Path(context['output_dir']) / 'events.jsonl'\n"
            "with target.open('a') as out: out.write(event['body'] + '\\n')\n"
        )
        (source / 'pack.json').write_text(json.dumps({
            'manifest_version': 1, 'id': PACK, 'version': '0.1.0',
            'requires': {'pack_api': 1, 'dependencies': []},
            'files': ['reader.py'],
            'entrypoints': {'reader': {'file': 'reader.py', 'interface': 'python-jsonl-v1',
                                       'input': {'path_prefixes': {ORIGIN: ['/api/']}},
                                       **({'delivery': delivery} if delivery else {})}},
            'config': {},
            'access': {'origins': [ORIGIN], 'capabilities': ['capture.read']},
        }))
        artifact = root / 'reader.tap-pack'
        build_artifact(source, artifact)
        profile = Profile(root, '/fixture/backend', 19000, 'explicit',
                          'http://fixture.test', [], bridge=dict(bridge_config(), enabled=False),
                          components={'version': 1, 'python': sys.executable,
                                      'bun': '/usr/bin/true', 'readers': {}, 'handlers': {}})
        profile.save()
        store = PackStore(root)
        store.install(artifact)
        store.enable(PACK, '0.1.0', origins=[ORIGIN], capabilities=['capture.read'])
        self.assertEqual(store.reader_path_prefixes(), {PACK: ((ORIGIN, ('/api/',)),)})
        stream = root / 'data/stream.jsonl'
        stream.parent.mkdir(parents=True, exist_ok=True)
        return stream

    def test_idle_reader_does_not_poll_and_wakes_on_growth_and_rotation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stream = self.make_profile(root)
            stream.write_bytes(record('/telemetry', 'noise') + record('/api/one', 'one'))
            events = root / 'data/readers' / PACK / 'events.jsonl'
            checkpoint = root / 'state/readers' / PACK / 'checkpoint.json'

            def delivered():
                return events.read_text().splitlines() if events.exists() else []

            process = subprocess.Popen(
                [sys.executable, '-B', '-m', 'tap_core.service', str(root)],
                cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                self.assertTrue(until(lambda: delivered() == ['one']))
                self.assertTrue(until(lambda: json.loads(checkpoint.read_text())['processed'] == 2))
                time.sleep(0.5)
                # Idle: the former loop rewrote the checkpoint every 0.25 s.
                before = checkpoint.stat().st_mtime_ns
                time.sleep(2.0)
                self.assertEqual(checkpoint.stat().st_mtime_ns, before)
                with stream.open('ab') as handle:
                    handle.write(record('/telemetry', 'noise') + record('/api/two', 'two'))
                self.assertTrue(until(lambda: delivered() == ['one', 'two'], timeout=3))
                os.rename(stream, root / 'data/stream.jsonl.1')
                stream.write_bytes(record('/api/three', 'three'))
                self.assertTrue(until(lambda: delivered() == ['one', 'two', 'three'], timeout=3))
            finally:
                process.terminate()
                process.communicate(timeout=10)


    def test_fresh_lane_drains_a_filtered_backlog_without_a_long_wait(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stream = self.make_profile(root, delivery='fresh-and-replay-v1')
            stream.write_bytes(b'')
            events = root / 'data/readers' / PACK / 'events.jsonl'
            process = subprocess.Popen(
                [sys.executable, '-B', '-m', 'tap_core.service', str(root)],
                cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                time.sleep(2.0)
                noise = b''.join(record(f'/telemetry/{i}', 'noise') for i in range(1200))
                with stream.open('ab') as handle:  # one write: then the stream stays quiet
                    handle.write(noise + record('/api/wanted', 'wanted'))
                started = time.monotonic()
                self.assertTrue(until(lambda: events.exists() and 'wanted' in events.read_text(), timeout=10))
                self.assertLess(time.monotonic() - started, 10)
            finally:
                process.terminate()
                process.communicate(timeout=10)

if __name__ == '__main__':
    unittest.main()
