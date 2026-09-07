"""Installed artifacts through startup preparation and the real Hub protocol."""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, PropertyMock, patch

from tap_core.bridge import effective_configuration, fingerprint
from tap_core.components import Job, _start
from tap_core.pack_store import PackStore, build_artifact
from tap_core.runtime import Profile

ROOT = Path(__file__).resolve().parents[1]

class InstalledBindingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='tap-installed-bindings-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.profile_root = self.root / 'profile'
        self.bridge = dict(version=1, enabled=True, hub_port=19201,
            allow_origins=[], exclude_origins=['https://excluded.example'], page_scripts=[])
        self.components = dict(version=1, python=sys.executable, bun='/usr/bin/true', readers={}, handlers={})
        self.profile = Profile(self.profile_root, '/fixture/backend', 19202, 'explicit',
            'http://fixture.example', [], bridge=self.bridge, components=self.components)
        self.profile.save()
        self.store = PackStore(self.profile_root)

    def artifact(self, version='0.1.0', prefix='linked:'):
        source = self.root / ('source-' + version)
        shutil.copytree(ROOT / 'fixtures/packs/installed-linked', source)
        path = source / 'pack.json'
        manifest = json.loads(path.read_text())
        manifest['version'] = version
        manifest['entrypoints'] = {'handler': manifest['entrypoints']['handler']}
        manifest['access'] = {'origins':['https://fixture.example','https://excluded.example'],
                              'capabilities':['bridge.handle']}
        # Change executable behavior, not defaults: update deliberately retains user config.
        handler = source / 'handler.py'
        handler.write_text(handler.read_text().replace('prefix + text', repr(prefix) + ' + text'))
        path.write_text(json.dumps(manifest))
        artifact = self.root / (version + '.tap-pack')
        build_artifact(source, artifact)
        return artifact

    def enable(self):
        self.store.enable('fixture.installed-linked', '0.1.0',
            origins=['https://fixture.example','https://excluded.example'], capabilities=['bridge.handle'])

    def test_handler_only_host_addon_parity_on_existing_origin(self):
        self.bridge['allow_origins'] = ['https://fixture.example','https://excluded.example']
        self.profile.save()
        self.store.install(self.artifact())
        self.enable()
        host = self.store.effective_bridge(self.bridge)
        addon = effective_configuration(self.profile_root, self.bridge)
        self.assertEqual(fingerprint(host), fingerprint(addon))
        self.assertEqual(host, addon)

    def test_start_refreshes_installed_handler_through_lifecycle(self):
        bun = os.environ.get('TAP_TEST_BUN') or shutil.which('bun')
        if not bun:
            self.skipTest('Set TAP_TEST_BUN for real installed-handler loopback')
        self.assertEqual(subprocess.check_output([bun,'--version'], text=True).strip(),'1.3.11')
        with socket.socket() as port:
            port.bind(('127.0.0.1',0))
            self.bridge['hub_port'] = port.getsockname()[1]
        self.components['bun'] = bun
        self.profile.save()
        self.store.install(self.artifact())
        self.enable()
        # Simulate only launchd; _start and the subsequent Hub execution are real.
        adapter = Mock()
        adapter.service_pid.return_value = None
        adapter.port_open.return_value = False
        adapter.service_loaded.return_value = False
        adapter.run.return_value = subprocess.CompletedProcess([],0,stdout='1.3.11\n')
        adapter.wait.return_value = True
        def start_and_probe(expected):
            with patch.object(Job,'plist',new_callable=PropertyMock,return_value=self.root/'components.plist'):
                _start(Profile.load(self.profile_root),adapter)
            result = subprocess.run([bun, str(ROOT/'tests/installed_handler_protocol.mjs'),
                str(self.profile_root),str(ROOT/'tap_core/hub.mjs'),expected],
                text=True,capture_output=True,timeout=20)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertTrue(json.loads(result.stdout)['ok'])
        start_and_probe('linked:')
        self.store.update(self.artifact('0.2.0','updated:'))
        start_and_probe('updated:')
        self.store.rollback('fixture.installed-linked')
        start_and_probe('linked:')
        self.store.disable('fixture.installed-linked')
        start_and_probe('disabled')
