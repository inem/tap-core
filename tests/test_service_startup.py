"""A slow or dead managed service does not hold back controller readiness (#247)."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest

from tap_core.runtime import Profile
from test_bridge import config as bridge_config

ROOT = Path(__file__).resolve().parents[1]
SERVE = ("import socket, sys, time\n"
         "time.sleep(float(sys.argv[2]))\n"
         "s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
         "s.bind(('127.0.0.1', int(sys.argv[1]))); s.listen(); time.sleep(120)\n")


def free_port():
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


def until(predicate, timeout=10):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return bool(predicate())


class ServiceStartupTests(unittest.TestCase):
    def test_slow_and_dead_services_degrade_without_blocking_readiness(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            slow, dead = free_port(), free_port()
            services = {
                'slow': {'command': [sys.executable, '-c', SERVE, str(slow), '2'], 'port': slow},
                # Never listens: mirrors codex-app-server missing its startup window.
                'dead': {'command': [sys.executable, '-c', 'import time; time.sleep(120)'], 'port': dead},
            }
            Profile(root, '/fixture/backend', 19000, 'explicit', 'http://fixture.test', [],
                    bridge=dict(bridge_config(), enabled=False),
                    components={'version': 1, 'python': sys.executable, 'bun': '/usr/bin/true',
                                'readers': {}, 'handlers': {}, 'services': services}).save()
            env = dict(os.environ, TAP_SERVICE_STARTUP_SECONDS='3')
            started = time.monotonic()
            process = subprocess.Popen([sys.executable, '-B', '-m', 'tap_core.service', str(root)],
                                       cwd=ROOT, env=env, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, text=True)

            def state():
                try:
                    return json.loads((root / 'state/components.json').read_text())
                except (OSError, ValueError):
                    return {}
            try:
                self.assertTrue(until(lambda: state().get('phase') == 'ready', timeout=5))
                # Ready well before either service could have passed a serial wait.
                self.assertLess(time.monotonic() - started, 1.9)
                self.assertEqual(state()['services']['slow']['phase'], 'starting')
                self.assertTrue(until(lambda: state()['services']['slow']['phase'] == 'ready'))
                slow_pid = state()['pid']
                self.assertTrue(until(lambda: state()['services']['dead']['phase'] == 'backoff'))
                self.assertTrue(state()['healthy'])
                self.assertFalse(state()['workloads_healthy'])
                self.assertEqual(state()['pid'], slow_pid)
            finally:
                process.terminate()
                output, _ = process.communicate(timeout=8)
            self.assertIn('service slow ready', output)
            self.assertIn('service dead not ready within 3s startup grace; degraded', output)
            self.assertLess(output.index('controller ready'), output.index('service slow ready'))


if __name__ == '__main__':
    unittest.main()
