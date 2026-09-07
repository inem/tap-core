#!/usr/bin/env python3
"""Opt-in live check: empty-root install → managed components → restart → purge.

Uses the existing installer. Supply absolute TAP_PYTHON / TAP_BACKEND / TAP_BUN to
avoid re-downloading when those pins are already on the machine; omit them to
exercise the installer's own downloads. Pass --local-checkout to feed this
working tree through the installer's archive path (needed before the branch is
pushed). Not a signed-release or CA-trust claim.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def run(command, env, timeout=300):
    result = subprocess.run(command, env=env, text=True, capture_output=True, timeout=timeout)
    if result.returncode != 0:
        raise SystemExit(f'command failed ({result.returncode}): {command}\n{result.stderr}\n{result.stdout}')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--python', type=Path)
    parser.add_argument('--backend', type=Path)
    parser.add_argument('--bun', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--local-checkout', action='store_true',
                        help='Serve this working tree as the installer checkout archive')
    args = parser.parse_args()
    report = {'scope': 'installer managed runtime; empty root; not clean-Mac matrix or CA trust',
              'source_commit': subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True).strip(),
              'local_checkout': args.local_checkout}
    parent = Path(tempfile.mkdtemp(prefix='tap-installer-managed-')).resolve()
    install_root = (parent / 'root')
    bin_dir = parent / 'bin'
    bin_dir.mkdir()
    # ownership.remove refuses non-canonical roots (/var → /private/var on macOS).
    env = {**os.environ, 'TAP_ROOT': str(install_root), 'TAP_BIN_DIR': str(bin_dir),
           'TAP_REF': report['source_commit'], 'TAP_PORT': '19191', 'TAP_HUB_PORT': '19192',
           'TAP_SKIP_START': '0', 'PATH': str(bin_dir) + os.pathsep + os.environ.get('PATH', '')}
    for name, value in (('TAP_PYTHON', args.python), ('TAP_BACKEND', args.backend), ('TAP_BUN', args.bun)):
        if value is not None:
            env[name] = str(value.resolve(strict=True))
    try:
        if args.local_checkout:
            archive = parent / 'checkout.tar.gz'
            with tarfile.open(archive, 'w:gz') as out:
                for name in ('tap', 'tap_core', 'instll', 'fixtures'):
                    out.add(ROOT / name, arcname='tap-core-local/' + name)
            fake_bin = parent / 'download-bin'
            fake_bin.mkdir()
            curl = fake_bin / 'curl'
            curl.write_text(
                '#!/bin/sh\n'
                'case "$*" in *api.github.com*) echo ' + 'a' * 40 + '; exit 0;; esac\n'
                'exec /bin/cat ' + shlex.quote(str(archive)) + '\n'
            )
            curl.chmod(0o700)
            env['PATH'] = str(fake_bin) + os.pathsep + env['PATH']
        installed = run(['/bin/bash', str(ROOT / 'instll/install')], env, timeout=600)
        report['install_ok'] = True
        report['install_log_tail'] = installed.stdout.strip().splitlines()[-12:]
        doctor = json.loads(run([str(bin_dir / 'tap'), 'doctor'], env).stdout)
        report['doctor_after_install'] = {
            'healthy': doctor.get('healthy'),
            'components': doctor.get('components', {}).get('healthy'),
            'bridge': doctor.get('bridge', {}).get('healthy'),
        }
        if not doctor.get('healthy'):
            raise SystemExit('doctor not healthy after install: ' + json.dumps(doctor, indent=2))
        components = json.loads((install_root / 'managed/components.json').read_text())
        if not all(str(p).startswith(str(install_root / 'checkout'))
                   for p in components['readers']['projection']['command'][1:]):
            raise SystemExit('reader script is outside installed checkout')
        run([str(bin_dir / 'tap'), 'off'], env)
        run([str(bin_dir / 'tap'), 'on'], env)
        doctor2 = json.loads(run([str(bin_dir / 'tap'), 'doctor'], env).stdout)
        report['doctor_after_restart'] = {
            'healthy': doctor2.get('healthy'),
            'components': doctor2.get('components', {}).get('healthy'),
        }
        if not doctor2.get('healthy'):
            raise SystemExit('doctor not healthy after restart')
        smoke = run([components['bun'], str(install_root / 'checkout/instll/smoke_hub.mjs'),
                     str(install_root / 'profile')], env)
        report['hub_smoke'] = json.loads(smoke.stdout.strip().splitlines()[-1])
        run(['/bin/bash', str(install_root / 'checkout/instll/uninstall')],
            {**env, 'TAP_PURGE': '1'})
        report['purged'] = not install_root.exists() and not (bin_dir / 'tap').exists()
        if not report['purged']:
            raise SystemExit('purge left install root or command behind')
        report['ok'] = True
    finally:
        if install_root.exists():
            try:
                run(['/bin/bash', str(install_root / 'checkout/instll/uninstall')],
                    {**env, 'TAP_PURGE': '1'}, timeout=120)
            except Exception:
                pass
        shutil.rmtree(parent, ignore_errors=True)
    text = json.dumps(report, indent=2)
    print(text)
    if args.output:
        args.output.write_text(text + '\n')
    return 0 if report.get('ok') else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
