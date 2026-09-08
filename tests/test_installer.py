"""Installer ownership/recovery regressions; no downloads or OS mutation."""
import hashlib
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
            for name in ('tap', 'tap_core', 'instll', 'fixtures'):
                out.add(REPO / name, arcname='tap-core-fixture/' + name)
        fake_bin = self.parent / 'download-bin'
        fake_bin.mkdir()
        curl = fake_bin / 'curl'
        curl.write_text('#!/bin/sh\ncase "$*" in *api.github.com*) echo ' + 'a' * 40 + '; exit 0;; esac\nexec /bin/cat ' + shlex.quote(str(archive)) + '\n')
        curl.chmod(0o700)
        backend = self.parent / 'backend'
        backend.write_text('#!/bin/sh\necho "Mitmproxy: 12.2.3"\n')
        backend.chmod(0o700)
        bun = self.parent / 'bun'
        bun.write_text('#!/bin/sh\necho 1.3.11\n')
        bun.chmod(0o700)
        env = {**os.environ, 'PATH': str(fake_bin) + os.pathsep + os.environ['PATH'],
               'TAP_ROOT': str(self.root), 'TAP_BIN_DIR': str(self.wrapper.parent),
               'TAP_PYTHON': sys.executable, 'TAP_BACKEND': str(backend), 'TAP_BUN': str(bun),
               'TAP_SKIP_START': '1'}
        installed = subprocess.run(['/bin/bash', str(REPO / 'instll/install')],
                                   env=env, capture_output=True, text=True)
        self.assertEqual(installed.returncode, 0, installed.stderr)
        metadata = json.loads((self.root / 'install.json').read_text())
        self.assertEqual(metadata['root'], str(self.root))
        self.assertFalse(metadata['profile_requested'])
        self.assertTrue(self.wrapper.is_file())
        managed = json.loads((self.root / 'managed/components.json').read_text())
        self.assertEqual(managed['bun'], str(bun))
        self.assertEqual(managed['python'], sys.executable)
        self.assertTrue(managed['readers']['projection']['command'][1].startswith(str(self.root / 'checkout')))
        bridge = json.loads((self.root / 'managed/bridge.json').read_text())
        self.assertEqual(bridge['hub_port'], 19000)
        self.assertTrue(bridge['page_scripts'][0].startswith(str(self.root / 'checkout')))
        removed = subprocess.run(['/bin/bash', str(self.root / 'checkout/instll/uninstall')],
                                 env={**env, 'TAP_PURGE': '1'}, capture_output=True, text=True)
        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertFalse(self.root.exists())
        self.assertFalse(self.wrapper.exists())
        self.assertTrue(backend.exists())

    def test_write_managed_rejects_missing_checkout_artifacts(self):
        write_spec = importlib.util.spec_from_file_location('write_managed', REPO / 'instll/write_managed.py')
        module = importlib.util.module_from_spec(write_spec)
        write_spec.loader.exec_module(module)
        with self.assertRaises(SystemExit):
            module.main(str(self.parent / 'missing'), str(self.parent), sys.executable,
                        sys.executable, '19000', str(self.parent / 'profile'))

    def test_write_managed_bindings_validate_as_profile_components(self):
        write_spec = importlib.util.spec_from_file_location('write_managed', REPO / 'instll/write_managed.py')
        module = importlib.util.module_from_spec(write_spec)
        write_spec.loader.exec_module(module)
        root = self.parent / 'managed-root'
        root.mkdir()
        checkout = root / 'checkout'
        for name in ('tap', 'tap_core', 'instll', 'fixtures'):
            target = checkout / name
            if (REPO / name).is_dir():
                shutil.copytree(REPO / name, target)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(REPO / name, target)
        bun = self.parent / 'bun-ok'
        bun.write_text('#!/bin/sh\necho 1.3.11\n')
        bun.chmod(0o700)
        module.main(str(root), str(checkout), sys.executable, str(bun), '19001', str(root / 'profile'))
        bridge = json.loads((root / 'managed/bridge.json').read_text())
        components = json.loads((root / 'managed/components.json').read_text())
        from tap_core.bridge import configuration as bridge_configuration
        from tap_core.components import configuration as components_configuration
        bridge_configuration(bridge)
        profile = Profile(root / 'profile', '/fixture/backend', 18999, 'explicit',
                          'http://example.com/', [], bridge=bridge)
        components_configuration(components, profile)

    def test_managed_harness_requires_runtime_overrides_for_local_checkout(self):
        harness = importlib.util.spec_from_file_location(
            'check_installer_managed_runtime', REPO / 'tools/check_installer_managed_runtime.py')
        module = importlib.util.module_from_spec(harness)
        harness.loader.exec_module(module)
        with self.assertRaisesRegex(SystemExit, 'requires --python'):
            module.main(['--local-checkout', '--output', str(self.parent / 'out.json')])

    def test_managed_harness_retains_root_when_purge_is_unverified(self):
        harness = importlib.util.spec_from_file_location(
            'check_installer_managed_runtime', REPO / 'tools/check_installer_managed_runtime.py')
        module = importlib.util.module_from_spec(harness)
        harness.loader.exec_module(module)
        recovery = self.root / 'profile/state/proxy-before.json'
        self.root.mkdir()
        recovery.parent.mkdir(parents=True)
        recovery.write_text('{"fixture":true}')
        (self.root / 'checkout/instll').mkdir(parents=True)
        (self.root / 'checkout/instll/uninstall').write_text('#!/bin/sh\nexit 1\n')
        (self.root / 'checkout/instll/uninstall').chmod(0o700)
        env = {'TAP_ROOT': str(self.root), 'TAP_PURGE': '1'}
        verified, error = module.attempt_purge(self.root, env, timeout=5)
        self.assertFalse(verified)
        self.assertTrue(recovery.is_file())
        self.assertTrue(error)

    def test_sudoers_snippet_passes_visudo_after_user_substitution(self):
        snippet = (REPO / 'instll/sudoers.snippet').read_text()
        self.assertIn('__TAP_USER__', snippet)
        rendered = self.parent / 'tap-core'
        rendered.write_text(snippet.replace('__TAP_USER__', 'fixtureuser'))
        check = subprocess.run(['visudo', '-c', '-f', str(rendered)], capture_output=True, text=True)
        self.assertEqual(check.returncode, 0, check.stderr)

    def test_update_preserves_profile_grants_routing_and_swaps_checkout(self):
        """A→B update keeps data/CA/routing; not purge/reinstall (#7 + #6 proxy)."""
        def pack(archive, marker):
            staging = self.parent / ('tree-' + marker)
            for name in ('tap', 'tap_core', 'instll', 'fixtures'):
                target = staging / name
                if (REPO / name).is_dir():
                    shutil.copytree(REPO / name, target)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(REPO / name, target)
            (staging / 'UPDATE_MARKER').write_text(marker + '\n')
            with tarfile.open(archive, 'w:gz') as out:
                out.add(staging, arcname='tap-core-' + marker)
        archive_a = self.parent / 'a.tar.gz'
        archive_b = self.parent / 'b.tar.gz'
        pack(archive_a, 'version-a')
        pack(archive_b, 'version-b')
        fake_bin = self.parent / 'download-bin'
        fake_bin.mkdir()
        curl = fake_bin / 'curl'
        # First install resolves commits API + serves archive A.
        curl.write_text(
            '#!/bin/bash\n'
            'case "$*" in\n'
            '  *api.github.com*) printf %s ' + 'a' * 40 + '; exit 0;;\n'
            'esac\n'
            'exec /bin/cat ' + shlex.quote(str(archive_a)) + '\n')
        curl.chmod(0o700)
        backend = self.parent / 'backend'
        backend.write_text('#!/bin/sh\necho "Mitmproxy: 12.2.3"\n')
        backend.chmod(0o700)
        bun = self.parent / 'bun'
        bun.write_text('#!/bin/sh\necho 1.3.11\n')
        bun.chmod(0o700)
        env = {**os.environ, 'PATH': str(fake_bin) + os.pathsep + os.environ['PATH'],
               'TAP_ROOT': str(self.root), 'TAP_BIN_DIR': str(self.wrapper.parent),
               'TAP_PYTHON': sys.executable, 'TAP_BACKEND': str(backend), 'TAP_BUN': str(bun),
               'TAP_SKIP_START': '1', 'TAP_ROUTING': 'explicit'}
        installed = subprocess.run(['/bin/bash', str(REPO / 'instll/install')],
                                   env=env, capture_output=True, text=True)
        self.assertEqual(installed.returncode, 0, installed.stderr)
        page = self.root / 'checkout/fixtures/managed/page.js'
        user_reader = self.root / 'checkout/fixtures/managed/handler.py'
        bridge = {
            'version': 1, 'enabled': True, 'hub_port': 19111,
            'allow_origins': ['http://127.0.0.1:18998'],
            'exclude_origins': ['https://keep.example'],
            'page_scripts': [str(page)],
        }
        components = {
            'version': 1, 'python': sys.executable, 'bun': str(bun),
            'readers': {
                'projection': {
                    'version': 1, 'revision': 'installer-managed-1',
                    'command': [sys.executable, str(self.root / 'checkout/fixtures/live-slice/reader.py')],
                    'config': {'url': 'http://127.0.0.1:18998/record'},
                },
                'custom': {
                    'version': 1, 'revision': 'user-1',
                    'command': [sys.executable, str(user_reader)],
                    'config': {'keep': True},
                },
            },
            'handlers': {
                'echo': {
                    'command': [sys.executable, str(user_reader)],
                    'config': {'echo': True},
                    'origins': ['http://127.0.0.1:18998'],
                },
            },
        }
        Profile(self.root / 'profile', str(backend), 18999, 'explicit',
                'http://fixture.test', [], bridge=bridge, components=components).save()
        retained = self.root / 'profile/data/retained.txt'
        retained.parent.mkdir(parents=True, exist_ok=True)
        retained.write_text('keep-me\n')
        ca = self.root / 'profile/certificates/mitmproxy-ca-cert.pem'
        ca.parent.mkdir(parents=True, exist_ok=True)
        # Minimal PEM so grant_ca path exists; fingerprint is recorded as digest arg.
        ca.write_text('-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n')
        ownership.grant_ca(str(self.root), str(ca), 'cafe' * 16)
        before = json.loads((self.root / 'install.json').read_text())
        self.assertEqual(before['routing'], 'explicit')
        self.assertEqual(before['grants']['ca']['sha256'], 'cafe' * 16)
        self.assertEqual((self.root / 'checkout/UPDATE_MARKER').read_text().strip(), 'version-a')

        updated = subprocess.run(['/bin/bash', str(self.root / 'checkout/instll/update')],
                                 env={**env, 'TAP_CHECKOUT_ARCHIVE': str(archive_b),
                                      'TAP_REF': 'b' * 40},
                                 capture_output=True, text=True)
        self.assertEqual(updated.returncode, 0, updated.stderr + updated.stdout)
        after = json.loads((self.root / 'install.json').read_text())
        self.assertEqual(after['ref'], 'b' * 40)
        self.assertEqual(after['routing'], 'explicit')
        self.assertEqual(after['grants']['ca']['sha256'], 'cafe' * 16)
        self.assertEqual(retained.read_text(), 'keep-me\n')
        self.assertEqual((self.root / 'checkout/UPDATE_MARKER').read_text().strip(), 'version-b')
        self.assertTrue(self.wrapper.is_file())
        self.assertEqual(hashlib.sha256(self.wrapper.read_bytes()).hexdigest(), after['wrapper_sha256'])
        leftovers = list(self.root.glob('checkout.prev.*'))
        self.assertEqual(leftovers, [], leftovers)
        profile = json.loads((self.root / 'profile/profile.json').read_text())
        self.assertEqual(profile['backend'], str(backend))
        self.assertEqual(profile['port'], 18999)
        self.assertEqual(profile['bridge']['hub_port'], 19111)
        self.assertEqual(profile['bridge']['exclude_origins'], ['https://keep.example'])
        self.assertIn('custom', profile['components']['readers'])
        self.assertEqual(profile['components']['bun'], str(bun))
        self.assertEqual(profile['components']['python'], sys.executable)
        managed = json.loads((self.root / 'managed/bridge.json').read_text())
        self.assertEqual(managed['hub_port'], 19000)

    def test_update_refuses_while_profile_lock_held(self):
        self.prepare(configured=True)
        shutil.copytree(REPO / 'instll', self.root / 'checkout/instll', dirs_exist_ok=True)
        shutil.copytree(REPO / 'tap_core', self.root / 'checkout/tap_core', dirs_exist_ok=True)
        (self.root / 'checkout/tap').write_text('#!/bin/sh\n')
        staging = self.parent / 'next-profile-lock'
        shutil.copytree(self.root / 'checkout', staging)
        from tap_core.runtime import profile_lock
        with profile_lock(self.root / 'profile'):
            result = subprocess.run(
                [sys.executable, str(staging / 'instll/ownership.py'), 'apply-checkout',
                 str(self.root), str(staging), sys.executable, '/fixture/backend', sys.executable,
                 'b' * 40, 'arm64', '19000'],
                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('changing this profile', result.stderr)

    def test_refresh_restores_mark_and_wrapper_on_save_failure(self):
        self.prepare()
        before_mark = (self.root / 'install.json').read_bytes()
        before_wrapper = self.wrapper.read_bytes()
        with patch.object(ownership, '_save_mark', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                ownership.refresh(str(self.root), sys.executable, '/fixture/backend', 'b' * 40, 'arm64')
        self.assertEqual((self.root / 'install.json').read_bytes(), before_mark)
        self.assertEqual(self.wrapper.read_bytes(), before_wrapper)

    def test_apply_restores_mark_when_policy_fails_after_refresh(self):
        self.prepare(configured=False)
        shutil.copytree(REPO / 'instll', self.root / 'checkout/instll', dirs_exist_ok=True)
        shutil.copytree(REPO / 'tap_core', self.root / 'checkout/tap_core', dirs_exist_ok=True)
        for name in ('fixtures/managed/page.js', 'fixtures/managed/handler.py',
                     'fixtures/live-slice/reader.py'):
            path = self.root / 'checkout' / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('fixture\n')
        (self.root / 'checkout/tap').write_text('ok-a\n')
        (self.root / 'checkout/UPDATE_MARKER').write_text('version-a\n')
        backend = self.parent / 'fixture-backend'
        backend.write_text('#!/bin/sh\necho "Mitmproxy: 12.2.3"\n')
        backend.chmod(0o700)
        staging = self.parent / 'policy-next'
        shutil.copytree(self.root / 'checkout', staging)
        (staging / 'UPDATE_MARKER').write_text('version-b\n')
        before_mark = json.loads((self.root / 'install.json').read_text())
        real_refresh = ownership.refresh

        def lie_routing(*args, **kwargs):
            data = real_refresh(*args, **kwargs)
            return dict(data, routing='system' if before_mark['routing'] == 'explicit' else 'explicit')

        with patch.object(ownership, 'refresh', side_effect=lie_routing):
            with self.assertRaises(ValueError) as ctx:
                ownership.apply_checkout(
                    str(self.root), str(staging), sys.executable, str(backend),
                    sys.executable, 'b' * 40, 'arm64', '19000')
        self.assertIn('routing changed', str(ctx.exception))
        self.assertEqual((self.root / 'checkout/UPDATE_MARKER').read_text().strip(), 'version-a')
        after_mark = json.loads((self.root / 'install.json').read_text())
        self.assertEqual(after_mark['ref'], before_mark['ref'])
        self.assertEqual(after_mark['routing'], before_mark['routing'])
        self.assertEqual(list(self.root.glob('checkout.prev.*')), [])

    def test_ensure_profile_stopped_refuses_unknown_state(self):
        self.prepare(configured=True)
        shutil.copytree(REPO / 'tap_core', self.root / 'checkout/tap_core', dirs_exist_ok=True)
        sys.path.insert(0, str(self.root / 'checkout'))
        with patch('tap_core.runtime.MacOS.service_loaded', side_effect=OSError('launchctl down')):
            with self.assertRaises(ValueError) as ctx:
                ownership._ensure_profile_stopped(self.root / 'profile')
        self.assertIn('unknown', str(ctx.exception).lower())

    def test_ensure_profile_stopped_runs_off_when_only_hub_busy(self):
        self.prepare(configured=True)
        page = self.parent / 'page.js'
        page.write_text('console.log(1)\n')
        bridge = {
            'version': 1, 'enabled': True, 'hub_port': 19111,
            'allow_origins': ['http://127.0.0.1:18998'],
            'exclude_origins': [], 'page_scripts': [str(page)],
        }
        components = {
            'version': 1, 'python': sys.executable, 'bun': sys.executable,
            'readers': {}, 'handlers': {},
        }
        Profile(self.root / 'profile', '/fixture/backend', 19998, 'explicit',
                'http://fixture.test', [], bridge=bridge, components=components).save()
        shutil.copytree(REPO / 'tap_core', self.root / 'checkout/tap_core', dirs_exist_ok=True)
        calls = []
        busy_states = [['hub service'], []]
        with patch.object(ownership, '_profile_processes_busy',
                          side_effect=lambda *a, **k: busy_states.pop(0)), \
             patch('tap_core.routing.ExplicitProxyRouting.recovery_pending', return_value=False), \
             patch('tap_core.routing.ExplicitProxyRouting.mutation_lock') as lock, \
             patch('tap_core.cli.mutate', side_effect=lambda *a, **k: calls.append('off') or 'stopped'):
            lock.return_value.__enter__ = lambda s: None
            lock.return_value.__exit__ = lambda *a: None
            self.assertTrue(ownership._ensure_profile_stopped(self.root / 'profile'))
        self.assertEqual(calls, ['off'])

    def test_restore_path_leaves_unreplaced_runtime_alone(self):
        destination = self.parent / 'python'
        destination.mkdir()
        (destination / 'bin').mkdir()
        (destination / 'bin' / 'python3').write_text('keep\n')
        ownership._restore_path(None, destination)
        self.assertEqual((destination / 'bin' / 'python3').read_text(), 'keep\n')

    def test_apply_restores_profile_when_policy_fails_after_propagate(self):
        self.prepare(configured=True)
        shutil.copytree(REPO / 'instll', self.root / 'checkout/instll', dirs_exist_ok=True)
        shutil.copytree(REPO / 'tap_core', self.root / 'checkout/tap_core', dirs_exist_ok=True)
        for name in ('fixtures/managed/page.js', 'fixtures/managed/handler.py',
                     'fixtures/live-slice/reader.py'):
            path = self.root / 'checkout' / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('fixture\n')
        (self.root / 'checkout/tap').write_text('ok-a\n')
        page = self.root / 'checkout/fixtures/managed/page.js'
        bridge = {
            'version': 1, 'enabled': True, 'hub_port': 19111,
            'allow_origins': ['http://127.0.0.1:18998'],
            'exclude_origins': ['https://keep.example'],
            'page_scripts': [str(page)],
        }
        components = {
            'version': 1, 'python': sys.executable, 'bun': sys.executable,
            'readers': {
                'custom': {
                    'version': 1, 'revision': 'user-1',
                    'command': [sys.executable, str(page)],
                    'config': {'keep': True},
                },
            },
            'handlers': {},
        }
        Profile(self.root / 'profile', '/fixture/backend', 19998, 'explicit',
                'http://fixture.test', [], bridge=bridge, components=components).save()
        before_profile = (self.root / 'profile/profile.json').read_bytes()
        backend = self.parent / 'fixture-backend-profile'
        backend.write_text('#!/bin/sh\necho "Mitmproxy: 12.2.3"\n')
        backend.chmod(0o700)
        # Seed managed so rename-backup path works.
        subprocess.run(
            [sys.executable, str(REPO / 'instll/write_managed.py'), str(self.root),
             str(self.root / 'checkout'), sys.executable, sys.executable, '19112',
             str(self.root / 'profile')],
            check=True, capture_output=True, text=True)
        staging = self.parent / 'profile-next'
        shutil.copytree(self.root / 'checkout', staging)
        (staging / 'UPDATE_MARKER').write_text('version-b\n')
        real_refresh = ownership.refresh

        def lie_routing(*args, **kwargs):
            data = real_refresh(*args, **kwargs)
            return dict(data, routing='system')

        with patch.object(ownership, '_ensure_profile_stopped', return_value=False), \
             patch.object(ownership, 'refresh', side_effect=lie_routing):
            with self.assertRaises(ValueError):
                ownership.apply_checkout(
                    str(self.root), str(staging), sys.executable, str(backend),
                    sys.executable, 'b' * 40, 'arm64', '19112')
        self.assertEqual((self.root / 'profile/profile.json').read_bytes(), before_profile)
        self.assertEqual((self.root / 'checkout/tap').read_text(), 'ok-a\n')

    def test_update_uses_target_ownership_when_current_lacks_apply(self):
        """main→B: current main has no verify/apply; target update still works."""
        def pack(archive, marker, *, mainlike):
            staging = self.parent / ('tree-' + marker)
            if staging.exists():
                shutil.rmtree(staging)
            for name in ('tap', 'tap_core', 'instll', 'fixtures'):
                source = REPO / name
                target = staging / name
                if source.is_dir():
                    shutil.copytree(source, target)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, target)
            if mainlike:
                # Exact main ownership surface: record/remove/grants only.
                main_ownership = subprocess.check_output(
                    ['git', 'show', 'main:instll/ownership.py'], cwd=REPO, text=True)
                (staging / 'instll/ownership.py').write_text(main_ownership)
                (staging / 'instll/update').unlink(missing_ok=True)
            (staging / 'UPDATE_MARKER').write_text(marker + '\n')
            with tarfile.open(archive, 'w:gz') as out:
                out.add(staging, arcname='tap-core-' + marker)
        archive_a = self.parent / 'mainlike.tar.gz'
        archive_b = self.parent / 'with-apply.tar.gz'
        pack(archive_a, 'version-a', mainlike=True)
        pack(archive_b, 'version-b', mainlike=False)
        self.assertNotIn("'verify'", (self.parent / 'tree-version-a/instll/ownership.py').read_text())
        fake_bin = self.parent / 'download-bin-main'
        fake_bin.mkdir()
        curl = fake_bin / 'curl'
        curl.write_text(
            '#!/bin/bash\n'
            'case "$*" in *api.github.com*) printf %s ' + 'a' * 40 + '; exit 0;; esac\n'
            'exec /bin/cat ' + shlex.quote(str(archive_a)) + '\n')
        curl.chmod(0o700)
        backend = self.parent / 'backend-main'
        backend.write_text('#!/bin/sh\necho "Mitmproxy: 12.2.3"\n')
        backend.chmod(0o700)
        bun = self.parent / 'bun-main'
        bun.write_text('#!/bin/sh\necho 1.3.11\n')
        bun.chmod(0o700)
        env = {**os.environ, 'PATH': str(fake_bin) + os.pathsep + os.environ['PATH'],
               'TAP_ROOT': str(self.root), 'TAP_BIN_DIR': str(self.wrapper.parent),
               'TAP_PYTHON': sys.executable, 'TAP_BACKEND': str(backend), 'TAP_BUN': str(bun),
               'TAP_SKIP_START': '1', 'TAP_ROUTING': 'explicit'}
        installed = subprocess.run(['/bin/bash', str(REPO / 'instll/install')],
                                   env=env, capture_output=True, text=True)
        self.assertEqual(installed.returncode, 0, installed.stderr)
        shutil.rmtree(self.root / 'checkout')
        with tarfile.open(archive_a, 'r:gz') as archive:
            archive.extractall(self.parent / 'extract-a')
        extracted = next((self.parent / 'extract-a').iterdir())
        extracted.rename(self.root / 'checkout')
        self.assertNotIn("'verify'", (self.root / 'checkout/instll/ownership.py').read_text())
        # Simulate curl|bash of the *target* update script against a main install.
        updated = subprocess.run(['/bin/bash', str(REPO / 'instll/update')],
                                 env={**env, 'TAP_CHECKOUT_ARCHIVE': str(archive_b),
                                      'TAP_REF': 'b' * 40},
                                 capture_output=True, text=True)
        self.assertEqual(updated.returncode, 0, updated.stderr + updated.stdout)
        self.assertEqual((self.root / 'checkout/UPDATE_MARKER').read_text().strip(), 'version-b')
        self.assertIn('def apply_checkout(', (self.root / 'checkout/instll/ownership.py').read_text())

    def test_update_refuses_while_root_lock_held(self):
        self.prepare(configured=True)
        shutil.copytree(REPO / 'instll', self.root / 'checkout/instll', dirs_exist_ok=True)
        shutil.copytree(REPO / 'tap_core', self.root / 'checkout/tap_core', dirs_exist_ok=True)
        (self.root / 'checkout/tap').write_text('#!/bin/sh\n')
        staging = self.parent / 'next'
        shutil.copytree(self.root / 'checkout', staging)
        from tap_core.runtime import profile_lock
        with profile_lock(self.root):
            result = subprocess.run(
                [sys.executable, str(self.root / 'checkout/instll/ownership.py'), 'apply-checkout',
                 str(self.root), str(staging), sys.executable, '/fixture/backend', sys.executable,
                 'b' * 40, 'arm64', '19000'],
                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('holds this root', result.stderr)

    def test_update_restores_checkout_when_managed_rewrite_fails(self):
        self.prepare(configured=False)
        shutil.copytree(REPO / 'instll', self.root / 'checkout/instll', dirs_exist_ok=True)
        shutil.copytree(REPO / 'tap_core', self.root / 'checkout/tap_core', dirs_exist_ok=True)
        for name in ('fixtures/managed/page.js', 'fixtures/managed/handler.py',
                     'fixtures/live-slice/reader.py'):
            path = self.root / 'checkout' / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('fixture\n')
        (self.root / 'checkout/tap').write_text('ok-a\n')
        (self.root / 'checkout/UPDATE_MARKER').write_text('version-a\n')
        backend = self.parent / 'fixture-backend-restore'
        backend.write_text('#!/bin/sh\necho "Mitmproxy: 12.2.3"\n')
        backend.chmod(0o700)
        staging = self.parent / 'broken-next'
        shutil.copytree(self.root / 'checkout', staging)
        (staging / 'UPDATE_MARKER').write_text('version-b\n')
        (staging / 'instll/write_managed.py').write_text('raise SystemExit("boom")\n')
        before = (self.root / 'checkout/UPDATE_MARKER').read_text()
        result = subprocess.run(
            [sys.executable, str(self.root / 'checkout/instll/ownership.py'), 'apply-checkout',
             str(self.root), str(staging), sys.executable, str(backend), sys.executable,
             'b' * 40, 'arm64', '19000'],
            capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('write_managed failed', result.stderr)
        self.assertEqual((self.root / 'checkout/UPDATE_MARKER').read_text(), before)
        self.assertEqual(list(self.root.glob('checkout.prev.*')), [])

    def test_update_refuses_foreign_wrapper(self):
        self.prepare()
        self.wrapper.write_text('foreign\n')
        result = subprocess.run(
            [sys.executable, str(REPO / 'instll/ownership.py'), 'verify', str(self.root)],
            capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('another owner', result.stderr)

    def test_installer_default_ref_is_main(self):
        text = (REPO / 'instll/install').read_text()
        self.assertRegex(text, r'REF="\$\{TAP_REF:-main\}"')
        self.assertNotRegex(text, r'REF="\$\{TAP_REF:-issue-49')

    def test_docs_and_hints_put_routing_env_on_bash_side_of_pipe(self):
        docs = (REPO / 'docs/install.md').read_text()
        self.assertIn('curl -fsSL https://instll.sh/inem/tap-core | TAP_ROUTING=explicit bash', docs)
        self.assertNotIn('TAP_ROUTING=explicit curl -fsSL https://instll.sh/inem/tap-core | sh', docs)

    def test_grant_sudoers_and_ca_roundtrip_in_ownership_record(self):
        self.prepare()
        ownership.grant_sudoers(str(self.root), '/etc/sudoers.d/tap-core-fixture', 'abc', 'TAP_CORE_PROXY_fixture')
        ownership.grant_ca(str(self.root), str(self.root / 'profile/certificates/mitmproxy-ca-cert.pem'), 'deadbeef')
        data = json.loads((self.root / 'install.json').read_text())
        self.assertEqual(data['grants']['sudoers']['alias'], 'TAP_CORE_PROXY_fixture')
        self.assertEqual(data['grants']['ca']['sha256'], 'deadbeef')

    def test_finish_setup_resolves_root_from_its_location(self):
        script = (REPO / 'instll/finish-setup').read_text()
        self.assertIn('HERE="$(cd "$(dirname "$0")" && pwd)"', script)
        self.assertIn('ROOT="$(cd "$HERE/../.." && pwd)"', script)
        self.assertNotIn('ROOT="${TAP_ROOT:-$HOME/.tap-core}"', script)

    def test_recovery_command_prefers_recorded_interpreter(self):
        harness = importlib.util.spec_from_file_location(
            'check_installer_managed_runtime', REPO / 'tools/check_installer_managed_runtime.py')
        module = importlib.util.module_from_spec(harness)
        harness.loader.exec_module(module)
        self.root.mkdir()
        (self.root / 'interpreter').write_text(sys.executable + '\n')
        (self.root / 'checkout/instll').mkdir(parents=True)
        (self.root / 'checkout/instll/ownership.py').write_text('pass\n')
        command = module.recovery_command(self.root)
        self.assertTrue(command.startswith(sys.executable))
        self.assertIn('ownership.py remove', command)

    def test_harness_emit_survives_cleanup_timeout(self):
        harness = importlib.util.spec_from_file_location(
            'check_installer_managed_runtime', REPO / 'tools/check_installer_managed_runtime.py')
        module = importlib.util.module_from_spec(harness)
        harness.loader.exec_module(module)
        self.root.mkdir()
        (self.root / 'interpreter').write_text(sys.executable + '\n')
        (self.root / 'checkout/instll').mkdir(parents=True)
        (self.root / 'checkout/instll/ownership.py').write_text('pass\n')
        out = self.parent / 'out.json'
        report = {'ok': False, 'cleanup_verified': False}
        with patch.object(module, 'attempt_purge',
                          side_effect=subprocess.TimeoutExpired(cmd='uninstall', timeout=1)):
            try:
                module.attempt_purge(self.root, {})
            except subprocess.TimeoutExpired as error:
                report['cleanup_error'] = type(error).__name__ + ': ' + str(error)
                report['retained_root'] = str(self.root)
                report['recovery'] = module.recovery_command(self.root)
        module.emit(report, out)
        data = json.loads(out.read_text())
        self.assertFalse(data['cleanup_verified'])
        self.assertEqual(data['retained_root'], str(self.root))
        self.assertIn('ownership.py remove', data['recovery'])
        self.assertIn('TimeoutExpired', data['cleanup_error'])

    def test_sudoers_helper_uses_unique_owned_dropin(self):
        script = (REPO / 'instll/enable-system-proxy-sudo').read_text()
        self.assertIn("tr 'a-z' 'A-Z'", script)
        self.assertIn('/etc/sudoers.d/tap-core-${tag}', script)
        self.assertIn('refusing to overwrite foreign or edited sudoers file', script)
        self.assertIn('refusing to follow sudoers symlink', script)
        self.assertIn('grant-sudoers', script)
        self.assertIn('tap-core-owned root=', script)

    def test_rendered_sudoers_alias_must_be_uppercase_for_visudo(self):
        root = '/tmp/tap-review-fixture'
        tag = hashlib.sha256(root.encode()).hexdigest()[:12]
        snippet = (REPO / 'instll/sudoers.snippet').read_text()
        for alias_tag, expect_ok in ((tag, False), (tag.upper(), True)):
            alias = 'TAP_CORE_PROXY_' + alias_tag
            body = '# tap-core-owned root=%s tag=%s\n' % (root, tag)
            body += '\n'.join(
                line.replace('__TAP_USER__', 'fixtureuser').replace('TAP_CORE_PROXY', alias)
                for line in snippet.splitlines() if line and not line.startswith('#')) + '\n'
            rendered = self.parent / ('sudoers-' + alias_tag)
            rendered.write_text(body)
            check = subprocess.run(['visudo', '-c', '-f', str(rendered)], capture_output=True, text=True)
            self.assertEqual(check.returncode == 0, expect_ok, check.stderr or check.stdout)

    def test_revoke_ca_requires_matching_fingerprint_without_security(self):
        self.prepare()
        cert = self.parent / 'ca.pem'
        key = self.parent / 'key.pem'
        subprocess.run(
            ['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-keyout', str(key), '-out', str(cert),
             '-days', '1', '-nodes', '-subj', '/CN=tap-fixture'],
            check=True, capture_output=True)
        fp = ownership.cert_fingerprint(cert)
        ownership.grant_ca(str(self.root), str(cert), fp)
        data = json.loads((self.root / 'install.json').read_text())
        calls = []
        def runner(command):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, '', '')
        # Missing cert → unresolved, no security call.
        cert.unlink()
        errors = ownership.revoke_grants(self.root, data, runner=runner)
        self.assertTrue(any('CA cert missing' in item for item in errors))
        self.assertFalse(any('security' in ' '.join(c) for c in calls))
        # Restored but wrong fingerprint → refuse, no security call.
        cert.write_text('-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n')
        # invalid PEM will error; use a different valid cert instead
        other = self.parent / 'other.pem'
        subprocess.run(
            ['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-keyout', str(key), '-out', str(other),
             '-days', '1', '-nodes', '-subj', '/CN=other'],
            check=True, capture_output=True)
        data['grants']['ca']['cert'] = str(other)
        calls.clear()
        errors = ownership.revoke_grants(self.root, data, runner=runner)
        self.assertTrue(any('fingerprint mismatch' in item for item in errors))
        self.assertFalse(any('security' in ' '.join(c) for c in calls))
        # Matching fingerprint → security invoked.
        data['grants']['ca'] = {'cert': str(other), 'sha256': ownership.cert_fingerprint(other)}
        calls.clear()
        errors = ownership.revoke_grants(self.root, data, runner=runner)
        self.assertEqual(errors, [])
        self.assertTrue(any('remove-trusted-cert' in ' '.join(c) for c in calls))

    def test_remove_preserves_root_when_grant_revoke_fails(self):
        self.prepare()
        ownership.grant_ca(str(self.root), str(self.root / 'missing.pem'), 'deadbeef')
        with self.assertRaisesRegex(ValueError, 'grant cleanup incomplete'):
            ownership.remove(str(self.root), '1')
        self.assertTrue(self.root.is_dir())
        self.assertTrue(self.wrapper.is_file())
        self.assertTrue((self.root / 'install.json').is_file())

    def test_enable_system_proxy_sudo_refuses_symlink_and_edited_dropin(self):
        self.prepare()
        shutil.copyfile(REPO / 'instll/enable-system-proxy-sudo', self.root / 'checkout/instll/enable-system-proxy-sudo')
        shutil.copyfile(REPO / 'instll/sudoers.snippet', self.root / 'checkout/instll/sudoers.snippet')
        (self.root / 'checkout/instll/enable-system-proxy-sudo').chmod(0o700)
        target_dir = self.parent / 'sudoers.d'
        target_dir.mkdir()
        fake_bin = self.parent / 'fake-bin'
        fake_bin.mkdir()
        sudo = fake_bin / 'sudo'
        # Fake sudo: cat/shasum/grep/install/visudo/-n -l for a sandbox target only.
        sudo.write_text(r'''#!/bin/bash
set -euo pipefail
# strip leading -n
args=()
for a in "$@"; do [ "$a" = "-n" ] && continue; args+=("$a"); done
set -- "${args[@]}"
case "$1" in
  cat) exec /bin/cat "$2" ;;
  grep) shift; exec /usr/bin/grep "$@" ;;
  shasum) shift; exec /usr/bin/shasum "$@" ;;
  install) shift; exec /usr/bin/install "$@" ;;
  visudo) shift; exec /usr/sbin/visudo "$@" ;;
  -l) exit 1 ;;
  *) echo "unexpected sudo: $*" >&2; exit 99 ;;
esac
''')
        sudo.chmod(0o700)
        env = {
            **os.environ,
            'PATH': str(fake_bin) + os.pathsep + os.environ.get('PATH', ''),
            'TAP_ROOT': str(self.root),
            'TAP_SUDOERS_USER': 'fixtureuser',
            'TAP_SUDOERS_PATH': str(target_dir / 'tap-core-link'),
        }
        # Symlink refusal.
        real = target_dir / 'real'
        real.write_text('x\n')
        (target_dir / 'tap-core-link').symlink_to(real)
        result = subprocess.run(['/bin/bash', str(self.root / 'checkout/instll/enable-system-proxy-sudo')],
                                env=env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('symlink', result.stderr)

        # Edited owned file refusal: comment matches but content differs from recorded+new.
        env['TAP_SUDOERS_PATH'] = str(target_dir / 'tap-core-edit')
        tag = hashlib.sha256(str(self.root).encode()).hexdigest()[:12]
        edited = target_dir / 'tap-core-edit'
        edited.write_text('# tap-core-owned root=%s tag=%s\n# admin edit\n' % (self.root, tag))
        ownership.grant_sudoers(str(self.root), str(edited), 'not-the-current-hash', 'TAP_CORE_PROXY_X')
        result = subprocess.run(['/bin/bash', str(self.root / 'checkout/instll/enable-system-proxy-sudo')],
                                env=env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('foreign or edited', result.stderr)
        self.assertEqual(edited.read_text(), '# tap-core-owned root=%s tag=%s\n# admin edit\n' % (self.root, tag))

        # Fresh install into empty path succeeds under fake sudo.
        env['TAP_SUDOERS_PATH'] = str(target_dir / 'tap-core-fresh')
        result = subprocess.run(['/bin/bash', str(self.root / 'checkout/instll/enable-system-proxy-sudo')],
                                env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        installed = (target_dir / 'tap-core-fresh').read_text()
        self.assertIn('tap-core-owned root=%s' % self.root, installed)
        self.assertRegex(installed, r'Cmnd_Alias TAP_CORE_PROXY_[0-9A-F]{12} =')
        data = json.loads((self.root / 'install.json').read_text())
        self.assertEqual(data['grants']['sudoers']['path'], str(target_dir / 'tap-core-fresh'))
        self.assertEqual(data['grants']['sudoers']['sha256'],
                         hashlib.sha256(installed.encode()).hexdigest())

    def test_finish_setup_uses_marker_wrapper_and_grants_ca(self):
        script = (REPO / 'instll/finish-setup').read_text()
        self.assertIn('data.get("root") == "$ROOT"', script)
        self.assertIn('WRAPPER=', script)
        self.assertIn('wrapper_sha256', script)
        self.assertIn('grant-ca', script)
        self.assertIn('TAP_ROOT="$ROOT" bash "$HERE/enable-system-proxy-sudo"', script)
        self.assertNotIn('HOME/.tap-core', script)
        self.assertNotIn('HOME/.local/bin', script)

    def test_install_hints_avoid_broken_routing_env_on_curl(self):
        text = (REPO / 'instll/install').read_text()
        self.assertIn('| TAP_ROUTING=explicit bash', text)
        self.assertNotIn('TAP_ROUTING=explicit curl', text)
        self.assertIn('tap routing set explicit', text)

