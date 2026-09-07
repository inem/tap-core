import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools.check_local_capture import check, extension_state, read_events


class LocalCaptureEvidenceTests(unittest.TestCase):
    def test_denied_extension_inspection_is_not_known_absence(self):
        with patch('tools.check_local_capture.run', return_value=subprocess.CompletedProcess([], 69, '', 'denied')):
            result = extension_state()
        self.assertTrue(result['inspection_failed'])
        self.assertEqual(result['inspection_exit'], 69)

    def test_partial_event_is_not_a_startup_ack(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'events.jsonl').write_text('{"event":"running"}')
            self.assertEqual(read_events(root), [])

    @unittest.skipUnless(sys.platform == 'darwin', 'macOS acceptance harness')
    def test_backend_failure_remains_failure_after_successful_direct_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            backend = Path(directory) / 'backend'
            backend.write_text('#!' + sys.executable + '\nimport sys\n'
                               'if "--version" in sys.argv: print("fixture backend")\n'
                               'else: sys.exit(7)\n')
            backend.chmod(0o700)
            args = SimpleNamespace(backend=str(backend), mode='local', startup_timeout=1, private_log=None)
            before = {'inspection_exit': 0, 'mitmproxy_entries': [], 'inspection_failed': False}
            after = dict(before, mitmproxy_entries=['mitmproxy [activated waiting for user]'])
            with patch('tools.check_local_capture.proxy_snapshot', return_value='fixture'), \
                    patch('tools.check_local_capture.extension_state', side_effect=[before, after]):
                result = check(args)
        self.assertEqual(result['evidence'], 'blocked_startup')
        self.assertEqual(result['blocker'], 'network_extension_awaiting_user_approval')
        self.assertEqual(result['backend_exit'], 7)
        self.assertTrue(result['after_stop_direct'])
        self.assertTrue(result['own_processes_stopped'])
        self.assertTrue(result['temporary_profile_removed'])

    @unittest.skipUnless(sys.platform == 'darwin', 'macOS acceptance harness')
    def test_running_hook_and_two_direct_responses_do_not_prove_local_capture(self):
        with tempfile.TemporaryDirectory() as directory:
            backend = Path(directory) / 'backend'
            backend.write_text('#!' + sys.executable + '\nimport json, os, sys, time\nfrom pathlib import Path\n'
                               'if "--version" in sys.argv: print("fixture backend"); sys.exit(0)\n'
                               'root=Path(json.loads(Path(os.environ["TAP_LOCAL_FIXTURE_CONFIG"]).read_text())["root"])\n'
                               '(root/"events.jsonl").write_text(json.dumps({"event":"running"})+"\\n")\n'
                               'time.sleep(30)\n')
            backend.chmod(0o700)
            args = SimpleNamespace(backend=str(backend), mode='local', startup_timeout=2, private_log=None)
            with patch('tools.check_local_capture.proxy_snapshot', return_value='fixture'), \
                    patch('tools.check_local_capture.extension_state', return_value={'mitmproxy_entries': []}):
                result = check(args)
        self.assertEqual(result['evidence'], 'failed_routing')
        self.assertFalse(result['cases'][1]['passed'])
        self.assertTrue(result['after_stop_direct'])
        self.assertTrue(result['own_processes_stopped'])
        self.assertTrue(result['temporary_profile_removed'])
