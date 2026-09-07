"""Delivery parity: independent subprocess baseline vs runner CLI (#9)."""
import json
import unittest
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch
from tools import check_reader_parity as parity

ROOT = Path(__file__).resolve().parents[1]


class ReaderParityTests(unittest.TestCase):
    def test_direct_uses_source_without_journal(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = parity.Profile(Path(directory), '/unused/backend', 18998,
                                     'explicit', 'http://fixture.test', [])
            profile.save()
            with patch('tap_core.journal.Journal.scan', side_effect=AssertionError('shared journal')):
                actual, _ = parity.direct_deliver(profile, [sys.executable, str(parity.FIXTURE)],
                                                   parity.corpus_bytes())
            self.assertEqual(actual, {'deliveries': ['A', 'B', 'A'], 'latest': 'A', 'receipts': 3})

    def test_corrupt_runner_input_fails_parity(self):
        original = parity.runner_deliver
        def corrupt(profile, definition, name='runner'):
            stream = profile.root / 'data/stream.jsonl'
            records = [json.loads(line) for line in stream.read_text().splitlines()]
            records[0]['body'] = json.dumps({'value': 'CORRUPTED'})
            stream.write_text(''.join(json.dumps(record) + '\n' for record in records))
            return original(profile, definition, name)
        with patch.object(parity, 'runner_deliver', side_effect=corrupt), patch.object(sys, 'argv', ['parity']):
            with self.assertRaisesRegex(SystemExit, 'parity failed'):
                parity.main()

    def test_corpus_is_stable_and_a_b_a_has_distinct_ids(self):
        source = parity.corpus_bytes()
        self.assertEqual(source, parity.corpus_bytes())
        records = [json.loads(line) for line in source.splitlines()]
        self.assertEqual(len({record['record_id'] for record in records}), 3)

    def test_check_reader_parity_report(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'parity.json'
            result = subprocess.run(
                [sys.executable, str(ROOT / 'tools/check_reader_parity.py'), '--output', str(output)],
                text=True, capture_output=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            report = json.loads(output.read_text())
        self.assertTrue(report['projection_equal'])
        self.assertTrue(report['order_equal'])
        self.assertEqual(report['corpus'], 'a_b_a_n3')
        self.assertFalse(report['baseline']['shares_reader_execute'])
        self.assertEqual(report['baseline']['direct'], 'independent_subprocess')
        self.assertEqual(report['direct']['deliveries'], ['A', 'B', 'A'])
        self.assertEqual(report['runner']['deliveries'], ['A', 'B', 'A'])
        self.assertEqual(report['direct']['latest'], 'A')
        self.assertTrue(report['commit'])
        self.assertIsInstance(report['source_dirty'], bool)
        self.assertFalse(report['baseline']['shares_journal_scan'])
        self.assertTrue(report['temporary_profile_removed'])
        self.assertEqual(len(report['digests']['reader_sha256']), 64)
        self.assertEqual(len(report['digests']['corpus_stream_sha256']), 64)
        self.assertEqual(len(report['digests']['corpus_values_sha256']), 64)
        self.assertIn('python', report['environment'])
        self.assertIn('platform', report['environment'])
        self.assertGreaterEqual(len(report['intentional_differences']), 3)
        cost = report['invocation_cost']
        self.assertEqual(cost['n_records'], 3)
        self.assertEqual(cost['model'], 'one_process_per_record')
        self.assertGreater(cost['runner_wall_ms_total'], 0)
        self.assertIn('next_threshold', cost)
