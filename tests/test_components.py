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
from tap_core.components import _start, configuration, identity, needs_hub, secret, status, stop, Job
from tap_core.pack_store import PackStore, build_artifact
from test_records_journal import capture_record, encoded
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

    def test_component_secret_translates_missing_and_invalid_files_to_tap_error(self):
        self.profile.components = self.binding
        self.profile.save()
        path = self.root / 'state/component-token'
        path.unlink()
        with self.assertRaisesRegex(TapError, 'Missing component-token'):
            secret(self.profile)
        path.write_text('invalid-secret')
        path.chmod(0o600)
        with self.assertRaisesRegex(TapError, 'Invalid component-token'):
            secret(self.profile)
        path.chmod(0o644)
        with self.assertRaisesRegex(TapError, 'component-token must be a private regular file'):
            secret(self.profile)

    def test_configuration_rejects_implicit_paths_and_nonfinite_handler_values(self):
        for field, value in [('python', 'python'), ('version', True), ('readers', []), ('unknown', None)]:
            with self.subTest(field=field), self.assertRaises(TapError):
                configuration(dict(self.binding, **{field: value}), self.profile)
        self.binding['handlers']['echo']['config'] = {'value': float('nan')}
        with self.assertRaises(TapError):
            configuration(self.binding, self.profile)

    def test_handler_binding_accepts_all_sites_origin(self):
        self.binding['handlers']['echo']['origins'] = ['*']
        self.assertEqual(configuration(self.binding, self.profile), self.binding)

    def test_enabled_bridge_requires_managed_hub_even_without_handlers(self):
        binding = dict(self.binding, handlers={})
        self.assertTrue(needs_hub(binding, self.profile.bridge))
        self.profile.components = None
        self.assertFalse(status(self.profile, Mock())['healthy'])
        with self.assertRaisesRegex(TapError, 'requires managed Hub components'):
            _start(self.profile, Mock())

    def test_failed_reader_does_not_make_live_control_plane_unhealthy(self):
        from unittest.mock import patch
        self.profile.components = self.binding
        self.profile.save()
        state = {
            'pid': 4321, 'updated_at': time.time(), 'configuration': identity(self.profile),
            'phase': 'ready', 'healthy': False, 'hub_pid': 9876, 'error': None,
            'readers': {
                'broken': {'healthy': False, 'phase': 'failed', 'failures': 3,
                           'error': 'JournalGap: retained segment unavailable'}
            }
        }
        (self.root / 'state/components.json').write_text(json.dumps(state))
        adapter = Mock()
        adapter.service_pid.return_value = 4321
        with patch('tap_core.components.hub_health', return_value={'pid': 9876}):
            observed = status(self.profile, adapter)
        self.assertTrue(observed['ready'])
        self.assertTrue(observed['healthy'])
        self.assertFalse(observed['workloads_healthy'])
        self.assertEqual(observed['readers']['broken']['phase'], 'failed')

    def test_fresh_managed_reader_handles_new_capture_while_replay_has_backlog(self):
        import sqlite3
        source = self.root / 'fresh-pack'
        source.mkdir()
        reader_source = (ROOT / 'fixtures/readers/sqlite_projection.py').read_text()
        (source / 'reader.py').write_text(reader_source.replace('    context = json.loads(',
            '    time.sleep(0.2)\n    context = json.loads(', 1))
        (source / 'pack.json').write_text(json.dumps({
            'manifest_version': 1, 'id': 'fixture.fresh', 'version': '0.1.0',
            'requires': {'pack_api': 1, 'dependencies': []}, 'files': ['reader.py'],
            'entrypoints': {'reader': {'file': 'reader.py', 'interface': 'python-jsonl-v1',
                                      'delivery': 'fresh-and-replay-v1'}},
            'config': {}, 'access': {'origins': ['https://fixture.example'],
                                     'capabilities': ['capture.read']},
        }))
        artifact = self.root / 'fresh.tap-pack'
        build_artifact(source, artifact)
        self.profile.bridge = dict(config(), enabled=False)
        self.profile.components = {'version': 1, 'python': sys.executable, 'bun': '/usr/bin/true',
                                   'readers': {}, 'handlers': {}}
        self.profile.save()
        store = PackStore(self.root)
        store.install(artifact)
        store.enable('fixture.fresh', '0.1.0', origins=['https://fixture.example'],
                     capabilities=['capture.read'])
        stream = self.root / 'data/stream.jsonl'
        stream.write_bytes(b''.join(encoded(capture_record(body=json.dumps({'value': f'old-{n}'})))
                                    for n in range(30)))
        process = subprocess.Popen([sys.executable, '-B', '-m', 'tap_core.service', str(self.root)],
                                   cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            fresh = self.root / 'state/readers/fixture.fresh/fresh-checkpoint.json'
            deadline = time.monotonic() + 12
            while not fresh.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(fresh.exists())
            with stream.open('ab') as handle:
                handle.write(encoded(capture_record(body=json.dumps({'value': 'new'}))))
            database = self.root / 'data/readers/fixture.fresh/projection.sqlite3'
            while time.monotonic() < deadline:
                if database.exists():
                    with sqlite3.connect(database) as connection:
                        rows = [json.loads(row[0])['value'] for row in
                                connection.execute('SELECT body FROM deliveries ORDER BY rowid')]
                    if 'new' in rows:
                        break
                time.sleep(0.05)
            else:
                self.fail('Fresh capture was not materialized')
            self.assertIn('new', rows[:3])
            replay = json.loads((self.root / 'state/readers/fixture.fresh/checkpoint.json').read_text())
            self.assertLess(replay['processed'], 30)
        finally:
            process.terminate()
            process.communicate(timeout=5)

    def test_component_identity_ignores_page_only_resource_changes(self):
        from unittest.mock import patch
        self.profile.components = self.binding
        first = dict(config(), page_scripts=['/packs/inspector/0.2.0/page.js'],
                     page_script_origins=[['https://www.linkedin.com']])
        second = dict(config(), page_scripts=['/packs/inspector/0.3.0/page.js'],
                      page_script_origins=[['https://www.linkedin.com']])
        with patch('tap_core.pack_store.PackStore.effective_components', return_value=self.binding), \
                patch('tap_core.pack_store.PackStore.effective_bridge', side_effect=[first, second]):
            self.assertEqual(identity(self.profile), identity(self.profile))

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
        for name, source in [('invalid','print("not JSON")'),
                             ('failed','import json; print(json.dumps({"ok":False,"error":{"code":"runtime_timeout","message":"Local runtime exceeded its time budget","details":{"phase":"base proposal","timeout_ms":60000,"secret":"must not reach page"}}})); raise SystemExit(2)'),
                             ('oversize','print("x"*300000)')]:
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
