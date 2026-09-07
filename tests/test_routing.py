"""Acceptance of the extracted routing boundary, with controlled OS effects."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from tap_core.cli import main, routing_set, status
from tap_core.routing import SystemProxyRouting, select_routing
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

    def test_same_mode_is_a_noop_without_touching_services_or_snapshot(self):
        profile = self.profile(mode='explicit')
        adapter = self.observations()
        result = routing_set(profile, adapter, 'explicit')
        self.assertIn('nothing changed', result)
        # Idempotent: never inspects/duplicates services, never writes a snapshot.
        adapter.service_loaded.assert_not_called()
        adapter.set_proxy.assert_not_called()
        self.assertFalse(profile.snapshot.exists())
        self.assertEqual(Profile.load(profile.root).routing, 'explicit')

    def test_stopped_profile_switch_commits_mode_and_stays_stopped(self):
        profile = self.profile(mode='explicit')
        adapter = self.observations()  # service_loaded False -> stopped
        result = routing_set(profile, adapter, 'system')
        self.assertEqual(Profile.load(profile.root).routing, 'system')
        # A stopped profile is only reconfigured; nothing is started or armed.
        adapter.start.assert_not_called()
        adapter.set_proxy.assert_not_called()
        self.assertIn('remains stopped', result)

    def test_running_switch_restores_old_mode_before_committing_then_starts_new(self):
        profile = self.profile(mode='system')
        adapter = self.observations()
        adapter.service_loaded.return_value = True  # running
        order = []
        def fake_off(self):
            order.append(('off', self.profile.routing))
        def fake_on(self):
            order.append(('on', self.profile.routing))
            return 'ON — new mode'
        with patch.object(Lifecycle, 'off', fake_off), patch.object(Lifecycle, 'on', fake_on):
            result = routing_set(profile, adapter, 'explicit')
        # off() must run under the OLD routing (restore old network), on() under the NEW.
        self.assertEqual(order, [('off', 'system'), ('on', 'explicit')])
        self.assertEqual(Profile.load(profile.root).routing, 'explicit')
        self.assertIn('ON — new mode', result)

    def test_startup_failure_after_commit_reports_committed_mode(self):
        profile = self.profile(mode='explicit')
        adapter = self.observations()
        adapter.service_loaded.return_value = True  # running
        def fake_off(self):
            pass
        def failing_on(self):
            raise TapError('Capture startup failed: boom; proxy settings were not changed')
        with patch.object(Lifecycle, 'off', fake_off), patch.object(Lifecycle, 'on', failing_on):
            with self.assertRaisesRegex(TapError, 'Routing set to system, but starting it failed'):
                routing_set(profile, adapter, 'system')
        # The mode is committed even though startup failed: honest partial state.
        self.assertEqual(Profile.load(profile.root).routing, 'system')

    def test_failed_recovery_leaving_system_does_not_commit_new_mode(self):
        profile = self.profile(mode='system')
        adapter = self.observations()
        adapter.service_loaded.return_value = True  # running
        def failing_off(self):
            raise TapError('Network recovery incomplete; service kept running')
        with patch.object(Lifecycle, 'off', failing_off):
            with self.assertRaisesRegex(TapError, 'recovery incomplete'):
                routing_set(profile, adapter, 'explicit')
        # New mode was NOT committed; the profile stays system for a retry.
        self.assertEqual(Profile.load(profile.root).routing, 'system')

    def test_generic_command_reloads_saved_mode_under_lock(self):
        # off/on/uninstall select the network lock and restore/enable from routing;
        # a mode saved between the pre-lock read and the lock must win. Model the
        # race: in-memory says explicit, but the saved profile is system.
        system_profile = self.profile('p', 'system')
        stale = Profile(system_profile.root, '/fixture/mitmdump', 18999, 'explicit', 'http://fixture.test', [])
        loads = []
        real_load = Profile.load.__func__
        def loader(root):
            loads.append(root)
            return stale if len(loads) == 1 else real_load(Profile, root)
        seen = {}
        def fake_mutate(command, profile, adapter):
            seen['routing'] = profile.routing
            return 'fixture'
        with patch('tap_core.cli.Profile.load', side_effect=loader), \
                patch('tap_core.cli.mutate', side_effect=fake_mutate), \
                patch('pathlib.Path.home', return_value=self.home), \
                patch('tap_core.cli.platform.system', return_value='Darwin'), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(['--profile', str(system_profile.root), 'off']), 0)
        # off acted on the reloaded system mode, not the stale explicit copy.
        self.assertEqual(seen['routing'], 'system')
        self.assertGreaterEqual(len(loads), 2)  # once before the lock, once under it

    def test_switch_into_system_from_explicit_takes_shared_network_lock(self):
        explicit = self.profile('x', 'explicit')
        with patch('pathlib.Path.home', return_value=self.home), \
                patch('tap_core.cli.platform.system', return_value='Darwin'), \
                patch('tap_core.cli.routing_set', return_value='fixture') as switch, \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            with SystemProxyRouting(explicit, self.observations()).mutation_lock():
                # explicit -> system must still contend for the shared network lock.
                self.assertEqual(main(['--profile', str(explicit.root), 'routing', 'set', 'system']), 1)
                switch.assert_not_called()
            self.assertEqual(main(['--profile', str(explicit.root), 'routing', 'set', 'system']), 0)
            self.assertEqual(switch.call_count, 1)
