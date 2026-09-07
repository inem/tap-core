"""Acceptance of the extracted routing boundary, with controlled OS effects."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from tap_core.cli import main, status
from tap_core.routing import select_routing
from tap_core.runtime import Lifecycle, MacOS, Profile, TapError


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.home = Path(self.directory.name)

    def profile(self, name='profile', mode='explicit'):
        profile = Profile(self.home / name, '/fixture/mitmdump', 18999, mode, 'http://fixture.test', [])
        profile.save()
        return profile

    def observations(self):
        adapter = Mock(spec=MacOS)
        adapter.service_loaded.return_value = False
        adapter.service_pid.return_value = None
        adapter.owns_port.return_value = False
        adapter.port_open.return_value = False
        return adapter

    def test_existing_profile_loads_without_migration_and_describes_limits(self):
        profile = self.profile()
        saved = (profile.root / 'profile.json').read_bytes()
        # Older profiles have no optional bridge field.
        old_config = json.loads(saved)
        old_config.pop('bridge', None)
        (profile.root / 'profile.json').write_text(json.dumps(old_config))
        before = (profile.root / 'profile.json').read_bytes()
        loaded = Profile.load(profile.root)
        adapter = self.observations()
        result = status(loaded, adapter)
        self.assertEqual(result['routing'], 'explicit')
        self.assertEqual(result['system_proxy_verified'], 'not_used')
        self.assertFalse(result['routing_adapter']['process_selection'])
        self.assertFalse(result['routing_adapter']['process_attribution'])
        self.assertFalse(result['routing_adapter']['network_extension_required'])
        self.assertFalse(result['port_open'])
        self.assertEqual((profile.root / 'profile.json').read_bytes(), before)
        adapter.services.assert_not_called()
        adapter.set_proxy.assert_not_called()

    def test_configured_system_capability_does_not_hide_unavailable_observation(self):
        profile = self.profile(mode='system')
        adapter = self.observations()
        adapter.services.side_effect = TapError('fixture inspection denied')
        result = status(profile, adapter)
        self.assertTrue(result['routing_adapter']['manages_system_settings'])
        self.assertIsNone(result['system_proxy_verified'])
        self.assertIn('system_proxy_verified', result['inspection_errors'])
        adapter.set_proxy.assert_not_called()

    def test_unsupported_choice_never_starts_or_falls_back(self):
        profile = self.profile()
        profile.routing = 'local'
        adapter = Mock(spec=MacOS)
        for command in ['install', 'on', 'off']:
            with self.subTest(command=command), self.assertRaisesRegex(TapError, 'no fallback'):
                getattr(Lifecycle(profile, adapter), command)()
        self.assertEqual(adapter.mock_calls, [])
        config = json.loads((profile.root / 'profile.json').read_text())
        config['routing'] = 'local'
        (profile.root / 'profile.json').write_text(json.dumps(config))
        with self.assertRaisesRegex(TapError, 'Unsupported profile'):
            Profile.load(profile.root)

    def test_system_commands_share_lock_but_explicit_does_not_acquire_it(self):
        first = self.profile('one', 'system')
        second = self.profile('two', 'system')
        explicit = self.profile('three', 'explicit')
        with patch('pathlib.Path.home', return_value=self.home), \
                patch('tap_core.cli.platform.system', return_value='Darwin'), \
                patch('tap_core.cli.mutate', return_value='fixture result') as mutate, \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            with select_routing(first, self.observations()).mutation_lock():
                self.assertEqual(main(['--profile', str(second.root), 'on']), 1)
                mutate.assert_not_called()
                self.assertEqual(main(['--profile', str(explicit.root), 'on']), 0)
                self.assertEqual(mutate.call_count, 1)
            self.assertEqual(main(['--profile', str(second.root), 'on']), 0)
            self.assertEqual(mutate.call_count, 2)
