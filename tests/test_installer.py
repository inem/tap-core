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
        self.assertIn('/etc/sudoers.d/tap-core-${tag}', script)
        self.assertIn('refusing to overwrite foreign sudoers file', script)
        self.assertIn('grant-sudoers', script)
        self.assertIn('tap-core-owned root=', script)
        self.assertNotRegex(script, r'TARGET=.*/etc/sudoers\.d/tap-core"')

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
