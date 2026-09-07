"""Managed configuration, ownership and guardian regressions with synthetic state."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock
from tap_core.runtime import Profile, MacOS, TapError
from tap_core.components import configuration, secret, stop, Job
from test_bridge import config

ROOT = Path(__file__).resolve().parents[1]

class ComponentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.profile = Profile(self.root, '/fixture/backend', 19000, 'explicit', 'http://fixture.test', [], bridge=config())
        self.binding = {'version': 1, 'python': sys.executable, 'bun': '/fixture/bun', 'readers': {},
                        'handlers': {'echo': {'command': [sys.executable, '-c', 'pass'], 'config': {}, 'origins': ['https://example.test']}}}

    def test_private_authority_is_distinct_and_stable_and_configuration_roundtrips(self):
        self.profile.components = self.binding
        self.profile.save()
        token = secret(self.profile)
        self.assertNotEqual(token, (self.root / 'state/bridge-token').read_text().strip())
        self.profile.save()
        self.assertEqual(secret(self.profile), token)
        self.assertNotIn(token, (self.root / 'profile.json').read_text())
        self.assertEqual(Profile.load(self.root).components, self.binding)

    def test_configuration_rejects_implicit_paths_and_nonfinite_handler_values(self):
        for field, value in [('python', 'python'), ('version', True), ('readers', []), ('unknown', None)]:
            with self.subTest(field=field), self.assertRaises(TapError):
                configuration(dict(self.binding, **{field: value}), self.profile)
        self.binding['handlers']['echo']['config'] = {'value': float('nan')}
        with self.assertRaises(TapError):
            configuration(self.binding, self.profile)

    def test_failed_start_cleanup_does_not_wait_on_foreign_listener(self):
        self.profile.components = self.binding
        adapter = Mock(spec=MacOS)
        adapter.service_loaded.return_value = False
        stop(self.profile, adapter)
        adapter.wait.assert_not_called()
        self.assertEqual(adapter.stop.call_args.args[0].label, self.profile.label + '.components')

    def test_guardian_cleans_child_after_controller_is_killed(self):
        pidfile = self.root / 'child.pid'
        controller = self.root / 'controller.py'
        controller.write_text('import os, subprocess, sys, time\n'
            'p=subprocess.Popen([sys.executable,"-B",' + repr(str(ROOT / 'tap_core/guardian.py')) + ',str(os.getpid()),sys.executable,"-c",'
            + repr('import os,pathlib,time; pathlib.Path(' + repr(str(pidfile)) + ').write_text(str(os.getpid())); time.sleep(60)')
            + '],start_new_session=True)\nprint(p.pid,flush=True)\ntime.sleep(60)\n')
        proc = subprocess.Popen([sys.executable, str(controller)], stdout=subprocess.PIPE, text=True)
        guard = int(proc.stdout.readline())
        try:
            deadline = time.monotonic() + 5
            while not pidfile.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(pidfile.exists())
            child = int(pidfile.read_text())
            proc.kill(); proc.wait(timeout=3)
            while time.monotonic() < deadline:
                try:
                    os.kill(child, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.05)
            else:
                self.fail('guarded child survived controller death')
        finally:
            if proc.poll() is None:
                proc.kill(); proc.wait(timeout=3)
            try:
                os.killpg(guard, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.stdout.close()

    def test_bun_protocol_and_handler_failures_over_real_loopback(self):
        import shutil
        import socket
        bun = os.environ.get('TAP_TEST_BUN') or shutil.which('bun')
        if not bun:
            self.skipTest('Set TAP_TEST_BUN to the declared Bun 1.3.11 executable')
        version = subprocess.check_output([bun, '--version'], text=True).strip()
        self.assertEqual(version, '1.3.11')
        with socket.socket() as reservation:
            reservation.bind(('127.0.0.1', 0))
            self.profile.bridge = config(hub_port=reservation.getsockname()[1],
                allow_origins=['https://example.test','https://excluded.test'], exclude_origins=['https://excluded.test'])
        self.binding['bun'] = bun
        handlers = self.binding['handlers']
        fixture = [sys.executable, '-B', str(ROOT / 'fixtures/managed/handler.py')]
        for name, value in [('echo', {'echo':True}), ('delayed', {'echo':True,'delay':0.6}), ('hang', {'echo':True,'delay':20})]:
            handlers[name] = {'command':fixture, 'config':value, 'origins':['https://example.test']}
        for name, source in [('invalid','print("not JSON")'), ('failed','raise SystemExit(2)'), ('oversize','print("x"*300000)')]:
            handlers[name] = {'command':[sys.executable,'-c',source], 'config':{}, 'origins':['https://example.test']}
        handlers['missing'] = {'command':[str(self.root/'missing')], 'config':{}, 'origins':['https://example.test']}
        handlers['denied'] = {'command':fixture, 'config':{}, 'origins':['https://elsewhere.test']}
        self.profile.components = self.binding
        self.profile.save()
        result = subprocess.run([bun, str(ROOT/'tests/managed_protocol.mjs'), str(self.root), str(ROOT/'tap_core/hub.mjs')],
                                capture_output=True, text=True, timeout=40)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        self.assertGreaterEqual(json.loads(result.stdout)['checks'], 24)
        failure = json.loads((self.root/'logs/handlers/failed/last-failure.json').read_text())
        self.assertEqual(failure['exit_code'], 2)

    def test_runtime_preflight_failure_unwinds_proxy_started_by_install(self):
        from tap_core.runtime import Lifecycle
        from unittest.mock import patch
        self.profile.components = self.binding
        adapter = Mock(spec=MacOS)
        adapter.port_open.return_value = False
        adapter.service_pid.return_value = None
        adapter.service_loaded.return_value = False
        with patch('tap_core.components.stop') as component_stop:
            with self.assertRaisesRegex(TapError, 'runtime is not executable'):
                Lifecycle(self.profile, adapter).install()
            adapter.stop.assert_called_once_with(self.profile)
            component_stop.assert_called_once_with(self.profile, adapter)

    def test_dependency_probe_failure_is_also_startup_failure(self):
        from tap_core.components import start
        from tap_core.runtime import StartupError
        from unittest.mock import patch
        self.profile.components = self.binding
        adapter = Mock(spec=MacOS)
        adapter.service_pid.return_value = None
        adapter.service_loaded.return_value = False
        adapter.port_open.return_value = False
        adapter.run.side_effect = TapError('runtime probe failed')
        with patch('tap_core.components.Path.is_file', return_value=True), patch('tap_core.components.os.access', return_value=True):
            with self.assertRaisesRegex(StartupError, 'runtime probe failed'):
                start(self.profile, adapter)
