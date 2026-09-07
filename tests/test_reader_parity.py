"""Delivery parity: direct one-record invocations vs runner CLI (#9)."""
import json
import unittest
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


class ReaderParityTests(unittest.TestCase):
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
        self.assertEqual(report['direct']['deliveries'], ['A', 'B', 'A'])
        self.assertEqual(report['runner']['deliveries'], ['A', 'B', 'A'])
        self.assertEqual(report['direct']['latest'], 'A')
        self.assertGreaterEqual(len(report['intentional_differences']), 3)
        cost = report['invocation_cost']
        self.assertEqual(cost['n_records'], 3)
        self.assertEqual(cost['model'], 'one_process_per_record')
        self.assertGreater(cost['runner_wall_ms_total'], 0)
        self.assertIn('next_threshold', cost)
