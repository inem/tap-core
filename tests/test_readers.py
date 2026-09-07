"""Real child readers with isolated state; no live network or launchd operations."""
import json
import os
from pathlib import Path
import sqlite3
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from tap_core import readers
from tap_core.journal import JournalGap
from tap_core.records import RecordError
from tap_core.readers import Reader, ReaderError, definition
from tap_core.runtime import Profile, TapError, profile_lock
from test_records_journal import capture_record, encoded

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'fixtures/readers/sqlite_projection.py'


class ReaderTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.profile = Profile(self.root, '/fixture/mitmdump', 18999, 'explicit', 'http://fixture.test', [])
        self.profile.save()
        self.stream = self.root / 'data/stream.jsonl'
        self.spec = {'version': 1, 'revision': 'fixture-1',
                     'command': [sys.executable, str(FIXTURE)], 'config': {}}
        self.reader = Reader(self.profile, 'one')

    def write(self, *values):
        records = [capture_record(body=json.dumps({'value': value})) for value in values]
        self.stream.write_bytes(b''.join(encoded(record) for record in records))
        return records

    def outputs(self, reader):
        with sqlite3.connect(reader.output / 'projection.sqlite3') as db:
            return [json.loads(row[0])['value'] for row in db.execute('SELECT body FROM deliveries ORDER BY rowid')]

    def test_two_readers_resume_at_independent_speeds(self):
        self.write('A', 'B', 'C')
        fast = Reader(self.profile, 'fast')
        self.reader.run(self.spec, max_records=1)
        fast.run(self.spec)
        fast_state = fast.checkpoint.read_bytes()
        self.assertEqual(self.outputs(self.reader), ['A'])
        self.assertEqual(self.outputs(fast), ['A', 'B', 'C'])
        # Recreate the controller object: only checkpoint files carry progress.
        slow = Reader(self.profile, 'one')
        self.assertEqual(slow.run(self.spec)['completed_this_run'], 2)
        self.assertEqual(self.outputs(slow), ['A', 'B', 'C'])
        self.assertEqual(fast.checkpoint.read_bytes(), fast_state)
        self.assertEqual(slow.run(self.spec)['completed_this_run'], 0)

    def test_new_reader_replays_retained_history_without_resetting_another(self):
        self.write('A', 'B')
        self.reader.run(self.spec)
        previous = self.reader.checkpoint.read_bytes()
        later = Reader(self.profile, 'later')
        later.run(self.spec)
        self.assertEqual(self.outputs(later), ['A', 'B'])
        self.assertEqual(self.reader.checkpoint.read_bytes(), previous)

    def test_failure_before_output_keeps_cursor_and_explicit_run_retries(self):
        self.write('A')
        spec = dict(self.spec, config={'fixture_mode': 'fail_before_once'})
        with self.assertRaisesRegex(ReaderError, 'code 3'):
            self.reader.run(spec)
        state = self.reader.load()
        self.assertIsNone(state['cursor'])
        self.assertIsNotNone(state['inflight'])
        self.assertEqual(state['phase'], 'failed')
        self.reader.run(spec)
        self.assertEqual(self.outputs(self.reader), ['A'])

    def test_failure_after_effect_is_deduplicated_by_reader_receipt(self):
        self.write('A')
        spec = dict(self.spec, config={'fixture_mode': 'fail_after_once'})
        with self.assertRaisesRegex(ReaderError, 'code 4'):
            self.reader.run(spec)
        self.assertEqual(self.outputs(self.reader), ['A'])
        self.assertIsNone(self.reader.load()['cursor'])
        self.reader.run(spec)
        self.assertEqual(self.outputs(self.reader), ['A'])
        self.assertEqual(self.reader.load()['processed'], 1)

    def test_checkpoint_failure_after_effect_never_advances_in_error_handler(self):
        self.write('A')
        real_save = readers.save
        failed = False
        def save(path, value):
            nonlocal failed
            if value['cursor'] is not None and not failed:
                failed = True
                raise OSError('fixture checkpoint write failure')
            return real_save(path, value)
        with patch.object(readers, 'save', side_effect=save):
            with self.assertRaises(OSError):
                self.reader.run(self.spec)
        self.assertIsNone(self.reader.load()['cursor'])
        self.assertEqual(self.outputs(self.reader), ['A'])
        self.reader.run(self.spec)
        self.assertEqual(self.outputs(self.reader), ['A'])

    def test_timeout_is_bounded_and_does_not_advance_cursor(self):
        self.write('A')
        spec = dict(self.spec, config={'fixture_mode': 'hang_after_once'})
        start = time.monotonic()
        with self.assertRaisesRegex(ReaderError, 'timed out'):
            self.reader.run(spec, timeout=0.2)
        self.assertLess(time.monotonic() - start, 3)
        self.assertIsNone(self.reader.load()['cursor'])
        self.reader.run(spec)
        self.assertEqual(self.outputs(self.reader), ['A'])

    def test_controller_crash_keeps_lock_until_child_exits(self):
        self.write('A')
        spec = dict(self.spec, config={'fixture_mode': 'hang_after_once'})
        path = self.root / 'crash-reader.json'
        path.write_text(json.dumps(spec))
        controller = subprocess.Popen([sys.executable, str(ROOT / 'tap'), '--profile', str(self.root),
                                       'reader', 'run', 'one', '--definition', str(path)],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        worker = None
        try:
            deadline = time.monotonic() + 5
            pidfile = self.reader.work / 'worker.pid'
            while not pidfile.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            worker = int(pidfile.read_text())
            controller.kill()
            controller.wait(timeout=3)
            with self.assertRaises(TapError):
                self.reader.run(spec)
            self.assertIsNone(self.reader.load()['cursor'])
        finally:
            if controller.poll() is None:
                controller.kill()
                controller.wait(timeout=3)
            if worker is not None:
                try:
                    os.killpg(worker, signal.SIGKILL)  # only this test's known child group
                except ProcessLookupError:
                    pass
        deadline = time.monotonic() + 3
        while True:
            try:
                self.reader.run(spec)
                break
            except TapError as error:
                if 'Another command' not in str(error) or time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)
        self.assertEqual(self.outputs(self.reader), ['A'])

    def test_large_child_output_is_bounded_and_not_success(self):
        self.write('A')
        with self.assertRaisesRegex(ReaderError, 'output limit'):
            self.reader.run(dict(self.spec, config={'fixture_mode': 'flood'}))
        self.assertIsNone(self.reader.load()['cursor'])
        self.assertLessEqual(sum(p.stat().st_size for p in self.reader.logs.iterdir()), readers.OUTPUT_LIMIT)

    def test_bad_record_stops_before_later_records(self):
        records = self.write('A', 'B')
        self.stream.write_bytes(encoded(records[0]) + b'{"record_version":99}\n' + encoded(records[1]))
        with self.assertRaises(RecordError):
            self.reader.run(self.spec)
        self.assertEqual(self.outputs(self.reader), ['A'])
        self.assertEqual(self.reader.load()['processed'], 1)

    def test_unfinished_tail_does_not_advance_and_later_completes(self):
        records = self.write('A', 'B')
        self.stream.write_bytes(encoded(records[0]) + encoded(records[1])[:-1])
        self.reader.run(self.spec)
        self.assertEqual(self.outputs(self.reader), ['A'])
        with self.stream.open('ab') as handle:
            handle.write(b'\n')
        self.reader.run(self.spec)
        self.assertEqual(self.outputs(self.reader), ['A', 'B'])

    def test_rotation_resume_and_retention_gap_require_explicit_replay(self):
        self.write('A')
        self.reader.run(self.spec)
        self.stream.rename(self.stream.with_name('stream.jsonl.1'))
        self.write('B')
        self.reader.run(self.spec)
        self.assertEqual(self.outputs(self.reader), ['A', 'B'])
        self.stream.unlink()
        self.write('C')
        with self.assertRaises(JournalGap):
            self.reader.run(self.spec)
        self.assertEqual(self.reader.load()['phase'], 'gap')
        self.reader.replay(self.spec)
        self.reader.run(self.spec)
        self.assertEqual(self.outputs(self.reader), ['A', 'B', 'C'])
        self.assertEqual(self.reader.load()['generation'], 2)

    def test_a_b_a_updates_latest_instead_of_deduplicating_by_content(self):
        self.write('A', 'B', 'A')
        self.reader.run(self.spec)
        self.assertEqual(self.outputs(self.reader), ['A', 'B', 'A'])
        with sqlite3.connect(self.reader.output / 'projection.sqlite3') as db:
            self.assertEqual(json.loads(db.execute('SELECT body FROM latest').fetchone()[0])['value'], 'A')
        self.reader.replay(self.spec)
        self.reader.run(self.spec)
        self.assertEqual(self.outputs(self.reader), ['A', 'B', 'A'])

    def test_changed_definition_requires_deliberate_new_generation(self):
        self.write('A')
        self.reader.run(self.spec)
        previous = self.reader.checkpoint.read_bytes()
        changed = dict(self.spec, revision='fixture-2')
        with self.assertRaisesRegex(ReaderError, 'definition changed'):
            self.reader.run(changed)
        self.assertEqual(self.reader.checkpoint.read_bytes(), previous)
        self.reader.replay(changed)
        self.reader.run(changed)
        self.assertEqual(self.reader.load()['generation'], 2)

    def test_same_reader_is_locked_but_other_reader_can_run(self):
        self.write('A')
        self.reader.prepare()
        with profile_lock(self.reader.state):
            with self.assertRaises(TapError):
                self.reader.run(self.spec)
            other = Reader(self.profile, 'other')
            other.run(self.spec)
            self.assertEqual(self.outputs(other), ['A'])

    def test_definition_validation_and_bad_checkpoint_do_not_reset_progress(self):
        path = self.root / 'reader.json'
        for value in [dict(self.spec, version=True), dict(self.spec, command=['python']),
                      dict(self.spec, config=[]), dict(self.spec, unknown=1)]:
            path.write_text(json.dumps(value))
            with self.assertRaises(ReaderError):
                definition(path)
        self.reader.prepare()
        self.reader.checkpoint.write_text('{broken')
        with self.assertRaises(ValueError):
            self.reader.run(self.spec)
        self.assertEqual(self.reader.checkpoint.read_text(), '{broken')

    def test_missing_command_and_unknown_status_are_explicit(self):
        self.assertFalse(self.reader.status()['configured'])
        self.assertFalse(self.reader.state.exists())
        self.write('A')
        with self.assertRaises(OSError):
            self.reader.run(dict(self.spec, command=['/missing/fixture/reader']))
        self.assertEqual(self.reader.load()['phase'], 'failed')
        self.assertIsNone(self.reader.load()['cursor'])

    def test_existing_pack_reader_runs_without_changing_pack_interface(self):
        self.write('A')
        spec = dict(self.spec, command=[sys.executable, str(ROOT / 'fixtures/packs/reader/reader.py')],
                    config={'prefix': 'existing-pack'})
        self.reader.run(spec)
        result = json.loads((self.reader.output / 'observations.jsonl').read_text())
        self.assertEqual(result, {'label': 'existing-pack', 'body': {'value': 'A'}})
        self.assertNotIn('value', json.dumps(self.reader.status()))

    def test_cli_run_status_and_replay_with_real_processes(self):
        self.write('A', 'B')
        path = self.root / 'reader.json'
        path.write_text(json.dumps(self.spec))
        prefix = [sys.executable, str(ROOT / 'tap'), '--profile', str(self.root), 'reader']
        def cli(*args):
            result = subprocess.run(prefix + list(args), text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)
        self.assertEqual(cli('run', 'one', '--definition', str(path), '--max-records', '1')['completed_this_run'], 1)
        self.assertEqual(cli('run', 'one', '--definition', str(path))['completed_this_run'], 1)
        self.assertEqual(cli('status', 'one')['progress']['processed'], 2)
        self.assertEqual(cli('replay', 'one', '--definition', str(path))['progress']['generation'], 2)
        self.assertEqual(cli('run', 'one', '--definition', str(path))['completed_this_run'], 2)
        self.assertEqual(self.outputs(self.reader), ['A', 'B'])


if __name__ == '__main__':
    unittest.main()
