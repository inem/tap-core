"""Delivery parity: independent subprocess baseline vs runner CLI (#9)."""
import json
import unittest
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


class ReaderParityTests(unittest.TestCase):
    def test_check_reader_parity_report(self):
        source = (ROOT / 'tools/check_reader_parity.py').read_text()
        self.assertNotIn('from tap_core.readers import Reader', source)
        self.assertNotIn('reader.execute(', source)
        self.assertIn('def invoke_independent', source)

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
