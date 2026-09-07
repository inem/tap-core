"""Installer ownership/recovery regressions; no downloads or OS mutation."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import sys
import tempfile
import tarfile
import unittest
from unittest.mock import patch

from tap_core.runtime import Profile, TapError

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location('installer_ownership', REPO / 'instll/ownership.py')
ownership = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ownership)


class InstallerTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='tap-installer-')
        self.addCleanup(directory.cleanup)
        self.parent = Path(directory.name).resolve()
        self.root = self.parent / "install ' spaced"
        self.wrapper = self.parent / 'bin/tap'
        self.wrapper.parent.mkdir()

    def prepare(self, configured=False):
        helper = self.root / 'checkout/instll/ownership.py'
        helper.parent.mkdir(parents=True)
        shutil.copyfile(REPO / 'instll/ownership.py', helper)
        self.addCleanup(patch.stopall)
        patch.object(ownership, '__file__', str(helper)).start()
        ownership.record([str(self.root), str(self.wrapper), sys.executable,
                          '/fixture/backend', 'inem/tap-core', 'fixture-ref', 'arm64',
                          '19998', 'explicit', '0' if configured else '1'])
        if configured:
            Profile(self.root / 'profile', '/fixture/backend', 19998, 'explicit',
                    'http://fixture.test', []).save()
        return helper

    def test_failed_lifecycle_preserves_recovery_runtime_and_command_even_for_purge(self):
        self.prepare(configured=True)
        recovery = self.root / 'profile/state/proxy-before.json'
        recovery.write_text('{"fixture":true}')
        with patch('tap_core.cli.mutate', side_effect=TapError('recovery failed')):
            with self.assertRaisesRegex(TapError, 'recovery failed'):
                ownership.remove(str(self.root), '1')
        self.assertTrue(recovery.is_file())
        self.assertTrue(self.wrapper.is_file())
        self.assertTrue((self.root / 'checkout').is_dir())

    def test_replaced_command_and_symlink_are_never_removed(self):
        self.prepare()
        self.wrapper.write_text('foreign tap')
        with self.assertRaisesRegex(ValueError, 'another owner'):
            ownership.remove(str(self.root), '1')
        self.wrapper.unlink()
        target = self.parent / 'foreign'
        target.write_text('foreign bytes')
        self.wrapper.symlink_to(target)
        with self.assertRaises(ValueError):
            ownership.remove(str(self.root), '1')
        self.assertEqual(target.read_text(), 'foreign bytes')
        self.assertTrue(self.wrapper.is_symlink())
        self.assertTrue(self.root.is_dir())

    def test_install_refuses_existing_root_or_foreign_wrapper_before_download(self):
        self.prepare()
        before = (self.root / 'install.json').read_bytes()
        env = {**os.environ, 'TAP_ROOT': str(self.root), 'TAP_BIN_DIR': str(self.wrapper.parent)}
        result = subprocess.run(['/bin/bash', str(REPO / 'instll/install')],
                                env=env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('root already exists', result.stderr)
        self.assertEqual((self.root / 'install.json').read_bytes(), before)
        other = self.parent / 'fresh'
        env['TAP_ROOT'] = str(other)
        result = subprocess.run(['/bin/bash', str(REPO / 'instll/install')],
                                env=env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Command already exists', result.stderr)
        self.assertFalse(other.exists())

    def test_command_exclusive_creation_and_shell_quoting(self):
        self.prepare()
        # Executable path and arguments containing shell syntax stay literal.
        (self.root / 'checkout/tap').write_text('import sys,json;print(json.dumps(sys.argv[1:]))')
        result = subprocess.run([str(self.wrapper), 'status', '$(not-executed)'],
                                text=True, capture_output=True, check=True)
        self.assertEqual(json.loads(result.stdout),
                         ['--profile', str(self.root / 'profile'), 'status', '$(not-executed)'])

    def test_missing_interpreter_or_config_does_not_permit_purge(self):
        self.prepare(configured=True)
        (self.root / 'interpreter').write_text('/missing/python\n')
        result = subprocess.run(['/bin/bash', str(REPO / 'instll/uninstall')],
                                env={**os.environ, 'TAP_ROOT': str(self.root), 'TAP_PURGE': '1'},
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(self.wrapper.exists())
        (self.root / 'profile/profile.json').unlink()
        with self.assertRaisesRegex(ValueError, 'configuration is missing'):
            ownership.remove(str(self.root), '1')
        self.assertTrue(self.root.exists())

    def test_files_only_purge_and_default_retention(self):
        self.prepare()
        ownership.remove(str(self.root), '0')
        self.assertFalse(self.wrapper.exists())
        self.assertTrue((self.root / 'install.json').is_file())
        ownership.remove(str(self.root), '1')
        self.assertFalse(self.root.exists())

    def test_profile_lock_blocks_remove_before_lifecycle(self):
        from tap_core.runtime import profile_lock
        self.prepare(configured=True)
        with profile_lock(self.root / 'profile'), patch('tap_core.cli.mutate') as mutate:
            with self.assertRaisesRegex(TapError, 'Another command'):
                ownership.remove(str(self.root), '1')
            mutate.assert_not_called()
        self.assertTrue(self.wrapper.exists())

    def test_dangling_wrapper_is_refused_without_touching_target(self):
        target = self.parent / 'missing-target'
        self.wrapper.symlink_to(target)
        result = subprocess.run(['/bin/bash', str(REPO / 'instll/install')],
                                env={**os.environ, 'TAP_ROOT': str(self.root),
                                     'TAP_BIN_DIR': str(self.wrapper.parent)},
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(target.exists())
        self.assertFalse(self.root.exists())

    def test_shell_bootstrap_and_installed_uninstaller_with_local_archive(self):
        # Exercise both delivered shell entrypoints and the generated ownership
        # record, substituting only the download and backend binary.
        archive = self.parent / 'checkout.tar.gz'
        with tarfile.open(archive, 'w:gz') as out:
            for name in ('tap', 'tap_core', 'instll'):
                out.add(REPO / name, arcname='tap-core-fixture/' + name)
        fake_bin = self.parent / 'download-bin'
        fake_bin.mkdir()
        curl = fake_bin / 'curl'
        curl.write_text('#!/bin/sh\ncase "$*" in *api.github.com*) echo ' + 'a' * 40 + '; exit 0;; esac\nexec /bin/cat ' + shlex.quote(str(archive)) + '\n')
        curl.chmod(0o700)
        backend = self.parent / 'backend'
        backend.write_text('#!/bin/sh\necho "Mitmproxy: 12.2.3"\n')
        backend.chmod(0o700)
        env = {**os.environ, 'PATH': str(fake_bin) + os.pathsep + os.environ['PATH'],
               'TAP_ROOT': str(self.root), 'TAP_BIN_DIR': str(self.wrapper.parent),
               'TAP_PYTHON': sys.executable, 'TAP_BACKEND': str(backend), 'TAP_SKIP_START': '1'}
        installed = subprocess.run(['/bin/bash', str(REPO / 'instll/install')],
                                   env=env, capture_output=True, text=True)
        self.assertEqual(installed.returncode, 0, installed.stderr)
        metadata = json.loads((self.root / 'install.json').read_text())
        self.assertEqual(metadata['root'], str(self.root))
        self.assertFalse(metadata['profile_requested'])
        self.assertTrue(self.wrapper.is_file())
        removed = subprocess.run(['/bin/bash', str(self.root / 'checkout/instll/uninstall')],
                                 env={**env, 'TAP_PURGE': '1'}, capture_output=True, text=True)
        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertFalse(self.root.exists())
        self.assertFalse(self.wrapper.exists())
        self.assertTrue(backend.exists())
