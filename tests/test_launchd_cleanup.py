"""Exercise real start/stop/lifecycle against a synthetic launchd boundary."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, PropertyMock

from tap_core.runtime import Lifecycle, MacOS, Profile, TapError
from test_runtime import NetworkFixture
from tap_core.routing import select_routing


class LaunchdFixture(NetworkFixture):
    start = MacOS.start
    stop = MacOS.stop

    def __init__(self):
        super().__init__()
        self.loaded = self.listening = False
        self.bootstrap_error = self.bootout_error = False

    def service_pid(self, profile):
        return None

    def service_loaded(self, profile):
        return self.loaded

    def owns_port(self, profile):
        return self.loaded and self.listening

    def port_open(self, profile):
        return self.owns_port(profile)

    def wait(self, predicate, seconds=15):
        return predicate()

    def run(self, args, **kwargs):
        self.events.append(args[1])
        if args[1] == 'bootstrap':
            self.loaded = True  # even a failed bootstrap may partially load
            if self.bootstrap_error:
                raise TapError('bootstrap failed')
        elif args[1] == 'bootout':
            if self.bootout_error:
                raise TapError('bootout failed')
            self.loaded = False
        else:
            raise AssertionError(args)


class LaunchdCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.profile = Profile(self.root, '/fixture/backend', 18999, 'explicit', 'http://fixture.test', [])
        self.profile.save()
        self.plist = self.root / 'agent.plist'
        override = patch.object(Profile, 'plist', new_callable=PropertyMock, return_value=self.plist)
        override.start()
        self.addCleanup(override.stop)
        self.adapter = LaunchdFixture()
        self.lifecycle = Lifecycle(self.profile, self.adapter)

    def test_bootstrap_failure_cleans_partial_job_for_install_and_on(self):
        for command in ('install', 'on'):
            with self.subTest(command=command):
                self.adapter.bootstrap_error = True
                with self.assertRaisesRegex(TapError, 'failed startup job and autoload removed'):
                    getattr(self.lifecycle, command)()
                self.assertFalse(self.plist.exists())
                self.assertFalse(self.adapter.loaded)

    def test_acquisition_timeout_cleans_job_and_plist(self):
        with self.assertRaisesRegex(TapError, 'did not acquire port'):
            self.lifecycle.on()
        self.assertFalse(self.plist.exists())
        self.assertFalse(self.adapter.loaded)

    def test_partial_plist_write_removes_temporary_file_without_bootstrap(self):
        def fail(profile):
            self.plist.with_suffix('.tmp').write_bytes(b'partial')
            raise OSError('disk full')
        with patch.object(self.adapter, 'write_plist', side_effect=fail):
            with self.assertRaisesRegex(TapError, 'disk full'):
                self.lifecycle.on()
        self.assertFalse(self.plist.with_suffix('.tmp').exists())
        self.assertNotIn('bootstrap', self.adapter.events)

    def test_failed_start_restores_system_routing_before_bootout(self):
        self.profile.routing = 'system'
        select_routing(self.profile, self.adapter).enable()
        self.adapter.events.clear()
        with self.assertRaisesRegex(TapError, 'previous proxy routing restored'):
            self.lifecycle.on()
        self.assertFalse(self.profile.snapshot.exists())
        self.assertFalse(self.adapter.loaded)
        self.assertEqual(self.adapter.events[-1], 'bootout')
        self.assertTrue(any(isinstance(event, tuple) and not event[-1] for event in self.adapter.events))

    def test_failed_routing_recovery_retains_job_and_plist(self):
        self.profile.routing = 'system'
        select_routing(self.profile, self.adapter).enable()
        self.adapter.fail_restore = True
        with self.assertRaisesRegex(TapError, 'rollback FAILED'):
            self.lifecycle.on()
        self.assertTrue(self.plist.exists())
        self.assertTrue(self.adapter.loaded)
        self.assertTrue(self.profile.snapshot.exists())
        self.assertNotIn('bootout', self.adapter.events)

    def test_bootout_failure_is_reported_but_autoload_removed(self):
        self.adapter.bootout_error = True
        with self.assertRaisesRegex(TapError, 'startup cleanup FAILED.*bootout failed'):
            self.lifecycle.on()
        self.assertFalse(self.plist.exists())
        self.assertTrue(self.adapter.loaded)

    def test_unlink_failure_still_boots_out_and_is_reported(self):
        self.adapter.loaded = True
        self.plist.write_bytes(b'fixture')
        unlink = Path.unlink
        def fail(path, *args, **kwargs):
            if path == self.plist:
                raise PermissionError('fixture denied')
            return unlink(path, *args, **kwargs)
        with patch.object(Path, 'unlink', fail):
            with self.assertRaisesRegex(TapError, 'cleanup FAILED.*fixture denied'):
                self.lifecycle.off()
        self.assertFalse(self.adapter.loaded)
        self.assertTrue(self.plist.exists())

    def test_off_removes_login_autoload_and_on_recreates_it(self):
        self.adapter.listening = True
        self.lifecycle.on()
        self.assertTrue(self.plist.exists())
        self.lifecycle.off()
        self.assertFalse(self.plist.exists())  # absent from next login's autoload set
        self.assertFalse(self.adapter.loaded)
        self.lifecycle.off()  # repeated off remains safe
        self.lifecycle.on()
        self.assertTrue(self.plist.exists())
        self.assertTrue(self.adapter.loaded)
