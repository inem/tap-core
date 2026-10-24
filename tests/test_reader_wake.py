"""Routed readers: woken by capture only for records inside their own filter."""
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
from tap_core.routes import RouteTable


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


def record(path, body, origin=ORIGIN):
    value = capture_record(body=body)
    value['url'] = origin + path
    return value


def append(root, *values, stream_name='stream.jsonl'):
    """Append like the capture writer: each record first, then its notification."""
    table = RouteTable(root / 'state')
    for value in values:
        with (root / 'data' / stream_name).open('ab') as handle:
            handle.write(encoded(value))
        table.notify(value)
    table.close()


class ReaderWakeTests(unittest.TestCase):
    def test_reader_sleeps_through_irrelevant_records_and_wakes_for_its_own(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
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
                                           'input': {'path_prefixes': {ORIGIN: ['/api/']}}}},
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
            stream.write_bytes(encoded(record('/telemetry', 'noise')) + encoded(record('/api/one', 'one')))
            events = root / 'data/readers' / PACK / 'events.jsonl'
            checkpoint = root / 'state/readers' / PACK / 'checkpoint.json'

            def delivered():
                return events.read_text().splitlines() if events.exists() else []

            process = subprocess.Popen(
                [sys.executable, '-B', '-m', 'tap_core.service', str(root)],
                cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                # Startup catches up from the cursor without any notification.
                self.assertTrue(until(lambda: delivered() == ['one']))
                self.assertTrue(until(lambda: json.loads(checkpoint.read_text())['processed'] == 2))
                self.assertTrue(until(lambda: (root / 'state/routes/components.json').exists()))
                time.sleep(3.0)  # past the one settle pass after attaching
                before = checkpoint.stat().st_mtime_ns
                # Records outside the filter (other origin, other path) never wake it.
                append(root, record('/telemetry', 'noise'), record('/x', 'other', 'https://other.example'))
                time.sleep(2.0)
                self.assertEqual(checkpoint.stat().st_mtime_ns, before)
                started = time.monotonic()
                append(root, record('/api/two', 'two'))
                self.assertTrue(until(lambda: delivered() == ['one', 'two'], timeout=3))
                self.assertLess(time.monotonic() - started, 1.5)  # no idle floor for routed readers
                os.rename(stream, root / 'data/stream.jsonl.1')
                append(root, record('/api/three', 'three'))
                self.assertTrue(until(lambda: delivered() == ['one', 'two', 'three'], timeout=3))
            finally:
                process.terminate()
                process.communicate(timeout=10)


if __name__ == '__main__':
    unittest.main()
