#!/usr/bin/env python3
"""Opt-in live Core update A→B on an empty install root (#7).

Installs ref A (must already contain instll/update), keeps a profile data marker,
updates to ref B, checks #6 proxy policy (routing + optional CA grant unchanged),
doctor, then purges. Always uses TAP_ROUTING=explicit (no system proxy / CA trust).

Not Local Capture, clean-Mac browser matrix, or HTTPS-without-k.
"""
import argparse
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]


def run(command, env, timeout=600):
    return subprocess.run(command, env=env, text=True, capture_output=True, timeout=timeout)


def require_ok(result, label):
    if result.returncode != 0:
        raise SystemExit(f'{label} failed ({result.returncode}):\n{result.stderr}\n{result.stdout}')
    return result


def resolve_sha(ref):
    result = subprocess.run(
        ['curl', '-fsSL', '-H', 'Accept: application/vnd.github.sha',
         f'https://api.github.com/repos/inem/tap-core/commits/{ref}'],
        text=True, capture_output=True, timeout=60)
    if result.returncode or len(result.stdout.strip()) != 40:
        raise SystemExit(f'cannot resolve ref {ref!r}: {result.stderr or result.stdout}')
    return result.stdout.strip()


def attempt_purge(install_root, env):
    uninstaller = install_root / 'checkout/instll/uninstall'
    if not uninstaller.is_file():
        return False, 'uninstaller missing'
    result = run(['/bin/bash', str(uninstaller)], {**env, 'TAP_PURGE': '1'}, timeout=180)
    if result.returncode != 0:
        return False, (result.stderr or result.stdout or 'uninstall failed').strip()
    if install_root.exists():
        return False, 'root still present'
    return True, None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ref-a', required=True, help='Install this published commit/branch (must include instll/update)')
    parser.add_argument('--ref-b', required=True, help='Update target commit/branch')
    parser.add_argument('--python', type=Path)
    parser.add_argument('--backend', type=Path)
    parser.add_argument('--bun', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()

    sha_a = resolve_sha(args.ref_a)
    sha_b = resolve_sha(args.ref_b)
    if sha_a == sha_b:
        raise SystemExit('ref-a and ref-b resolve to the same commit; push a distinct B first')

    parent = Path(tempfile.mkdtemp(prefix='tap-update-live-')).resolve()
    install_root = parent / 'root'
    bin_dir = parent / 'bin'
    bin_dir.mkdir()
    env = {**os.environ,
           'TAP_ROOT': str(install_root),
           'TAP_BIN_DIR': str(bin_dir),
           'TAP_REF': sha_a,
           'TAP_PORT': '19291',
           'TAP_HUB_PORT': '19292',
           'TAP_ROUTING': 'explicit',
           'TAP_SKIP_START': '0',
           'PATH': str(bin_dir) + os.pathsep + os.environ.get('PATH', '')}
    for name, value in (('TAP_PYTHON', args.python), ('TAP_BACKEND', args.backend), ('TAP_BUN', args.bun)):
        if value is not None:
            env[name] = str(value.resolve(strict=True))

    report = {
        'scope': 'live empty-root install A → update B → purge; explicit routing; not Local Capture / CA trust / clean-Mac browser',
        'host': {
            'architecture': platform.machine(),
            'macos': platform.mac_ver()[0],
            'python': platform.python_version(),
            'platform': platform.platform(),
        },
        'ref_a': args.ref_a,
        'ref_b': args.ref_b,
        'sha_a': sha_a,
        'sha_b': sha_b,
        'ok': False,
        'cleanup_verified': False,
    }
    try:
        require_ok(run(['/bin/bash', str(ROOT / 'instll/install')], env, timeout=900), 'install A')
        marker = json.loads((install_root / 'install.json').read_text())
        report['install_ref'] = marker['ref']
        report['routing_before'] = marker['routing']
        report['ca_before'] = (marker.get('grants') or {}).get('ca')
        if marker['ref'] != sha_a:
            raise SystemExit(f'install recorded {marker["ref"]}, expected {sha_a}')
        if not (install_root / 'checkout/instll/update').is_file():
            raise SystemExit('installed A lacks instll/update')

        retained = install_root / 'profile/data/update-live-marker.txt'
        retained.parent.mkdir(parents=True, exist_ok=True)
        token = f'live-{int(time.time())}'
        retained.write_text(token + '\n')

        doctor = json.loads(require_ok(run([str(bin_dir / 'tap'), 'doctor'], env), 'doctor').stdout)
        report['doctor_after_install'] = {'healthy': doctor.get('healthy')}
        if not doctor.get('healthy'):
            raise SystemExit('doctor unhealthy after install A')

        update_env = {**env, 'TAP_REF': sha_b, 'TAP_SKIP_START': '0'}
        updated = require_ok(run(['/bin/bash', str(install_root / 'checkout/instll/update')],
                                 update_env, timeout=900), 'update B')
        report['update_log_tail'] = updated.stdout.strip().splitlines()[-20:]
        after = json.loads((install_root / 'install.json').read_text())
        report['update_ref'] = after['ref']
        report['routing_after'] = after['routing']
        report['ca_after'] = (after.get('grants') or {}).get('ca')
        if after['ref'] != sha_b:
            raise SystemExit(f'update recorded {after["ref"]}, expected {sha_b}')
        if after['routing'] != marker['routing']:
            raise SystemExit('routing changed during update')
        if report['ca_before'] != report['ca_after']:
            raise SystemExit('CA grant changed during update')
        if retained.read_text().strip() != token:
            raise SystemExit('profile data marker lost during update')
        report['profile_data_retained'] = True
        report['policy_proxy_hash6'] = {
            'routing_unchanged': True,
            'ca_grant_unchanged': True,
            'local_capture': False,
        }

        doctor2 = json.loads(require_ok(run([str(bin_dir / 'tap'), 'doctor'], env), 'doctor after update').stdout)
        report['doctor_after_update'] = {'healthy': doctor2.get('healthy')}
        if not doctor2.get('healthy'):
            raise SystemExit('doctor unhealthy after update')

        verified, error = attempt_purge(install_root, env)
        report['cleanup_verified'] = verified
        if not verified:
            raise SystemExit('purge failed: ' + str(error))
        report['ok'] = True
    except SystemExit as error:
        report['error'] = str(error)
    except Exception as error:
        report['error'] = type(error).__name__ + ': ' + str(error)
    finally:
        if install_root.exists():
            verified, error = attempt_purge(install_root, env)
            report['cleanup_verified'] = verified
            if not verified:
                report['cleanup_error'] = error
                report['retained_root'] = str(install_root)

    text = json.dumps(report, indent=2) + '\n'
    print(text)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text)
    return 0 if report.get('ok') and report.get('cleanup_verified') else 1


if __name__ == '__main__':
    raise SystemExit(main())
