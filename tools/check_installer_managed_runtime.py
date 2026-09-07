#!/usr/bin/env python3
"""Opt-in live check: empty-root install → managed components → restart → purge.

Uses the existing installer. Default path fetches the current HEAD commit archive
from GitHub (branch must be pushed). Omit --bun / --python / --backend to exercise
the installer's own downloads for those runtimes.

--local-checkout feeds this working tree as the Core archive only. It requires
explicit --python/--backend/--bun overrides so fake curl never substitutes runtime
downloads. Not a signed-release, CA-trust, or clean-Mac matrix claim.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import shlex
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def run(command, env, timeout=300):
    return subprocess.run(command, env=env, text=True, capture_output=True, timeout=timeout)


def require_ok(result, command):
    if result.returncode != 0:
        raise SystemExit(f'command failed ({result.returncode}): {command}\n{result.stderr}\n{result.stdout}')
    return result


def version_of(command, env=None):
    result = subprocess.run(command, env=env, text=True, capture_output=True, timeout=30)
    return (result.stdout or result.stderr).strip().splitlines()[0] if result.returncode == 0 else None


def tree_digest(paths):
    digest = hashlib.sha256()
    for path in paths:
        for file in sorted(path.rglob('*')):
            if file.is_file():
                digest.update(str(file.relative_to(ROOT)).encode())
                digest.update(file.read_bytes())
    return digest.hexdigest()


def write_selective_curl(fake_bin, archive, real_curl):
    """Intercept only Core commit/archive URLs; forward everything else."""
    fake_bin.mkdir(parents=True, exist_ok=True)
    curl = fake_bin / 'curl'
    curl.write_text(f'''#!/bin/bash
set -euo pipefail
args=("$@")
joined="${{args[*]}}"
case "$joined" in
  *api.github.com/repos/*/commits*)
    printf '%s' '{"a" * 40}'
    exit 0
    ;;
  *codeload.github.com/*|*github.com/*/archive/*|*github.com/*/tarball/*)
    exec /bin/cat {shlex.quote(str(archive))}
    ;;
esac
exec {shlex.quote(str(real_curl))} "$@"
''')
    curl.chmod(0o700)
    return curl


def attempt_purge(install_root, env, timeout=120):
    uninstaller = install_root / 'checkout/instll/uninstall'
    if not uninstaller.is_file():
        return False, 'installed uninstaller missing'
    try:
        result = run(['/bin/bash', str(uninstaller)], {**env, 'TAP_PURGE': '1'}, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        return False, f'uninstall timeout after {error.timeout}s'
    except OSError as error:
        return False, f'uninstall OSError: {error}'
    if result.returncode != 0:
        return False, (result.stderr or result.stdout or 'uninstall failed').strip()
    if install_root.exists():
        return False, 'install root still present after purge'
    return True, None


def recovery_command(install_root):
    interpreter = install_root / 'interpreter'
    python = interpreter.read_text().strip() if interpreter.is_file() else None
    ownership = install_root / 'checkout/instll/ownership.py'
    if python and Path(python).is_file() and ownership.is_file():
        return f'{python} {ownership} remove {install_root} 0'
    uninstall = install_root / 'checkout/instll/uninstall'
    if uninstall.is_file():
        return f'TAP_ROOT={install_root} bash {uninstall}'
    return f'inspect retained install at {install_root}'


def emit(report, output):
    text = json.dumps(report, indent=2)
    print(text)
    if output:
        output.write_text(text + '\n')
    if report.get('retained_root'):
        print('tap-core: retained ' + report['retained_root'] + ' for inspection/recovery', file=sys.stderr)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--python', type=Path)
    parser.add_argument('--backend', type=Path)
    parser.add_argument('--bun', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--local-checkout', action='store_true',
                        help='Serve this working tree as the Core archive; requires all runtime overrides')
    args = parser.parse_args(argv)

    dirty = subprocess.check_output(['git', '-C', str(ROOT), 'status', '--porcelain'], text=True)
    head = subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True).strip()
    report = {
        'scope': 'installer managed runtime; empty root; not clean-Mac matrix or CA trust',
        'base_commit': head,
        'worktree_dirty': bool(dirty.strip()),
        'archived_tree_sha256': None,
        'local_checkout': args.local_checkout,
        'architecture': platform.machine(),
        'macos': platform.mac_ver()[0],
        'routing': 'explicit',
        'runtime_modes': {},
        'cleanup_verified': False,
        'ok': False,
    }
    if args.local_checkout and not all((args.python, args.backend, args.bun)):
        raise SystemExit('--local-checkout requires --python, --backend and --bun '
                         '(fake curl must not substitute runtime downloads)')

    parent = Path(tempfile.mkdtemp(prefix='tap-installer-managed-')).resolve()
    install_root = parent / 'root'
    bin_dir = parent / 'bin'
    bin_dir.mkdir()
    report['harness_parent'] = str(parent)
    env = {**os.environ,
           'TAP_ROOT': str(install_root),
           'TAP_BIN_DIR': str(bin_dir),
           'TAP_REF': head if not args.local_checkout else 'local-checkout-fixture',
           'TAP_PORT': '19191',
           'TAP_HUB_PORT': '19192',
           'TAP_ROUTING': 'explicit',
           'TAP_SKIP_START': '0',
           'PATH': str(bin_dir) + os.pathsep + os.environ.get('PATH', '')}
    for name, value in (('TAP_PYTHON', args.python), ('TAP_BACKEND', args.backend), ('TAP_BUN', args.bun)):
        if value is not None:
            env[name] = str(value.resolve(strict=True))
            report['runtime_modes'][name] = 'reuse:' + env[name]
        else:
            report['runtime_modes'][name] = 'download'

    interrupted = None
    try:
        if args.local_checkout:
            archive = parent / 'checkout.tar.gz'
            members = [ROOT / name for name in ('tap', 'tap_core', 'instll', 'fixtures')]
            report['archived_tree_sha256'] = tree_digest(members)
            with tarfile.open(archive, 'w:gz') as out:
                for path in members:
                    out.add(path, arcname='tap-core-local/' + path.name)
            real_curl = shutil.which('curl')
            if not real_curl:
                raise SystemExit('curl required to forward non-Core downloads')
            fake_bin = parent / 'download-bin'
            write_selective_curl(fake_bin, archive, real_curl)
            env['PATH'] = str(fake_bin) + os.pathsep + env['PATH']

        installed = require_ok(run(['/bin/bash', str(ROOT / 'instll/install')], env, timeout=600),
                               'installer')
        report['install_ok'] = True
        report['install_log_tail'] = installed.stdout.strip().splitlines()[-16:]
        marker = json.loads((install_root / 'install.json').read_text())
        report['install_ref_recorded'] = marker.get('ref')
        components = json.loads((install_root / 'managed/components.json').read_text())
        report['installed_versions'] = {
            'python': version_of([components['python'], '--version']),
            'bun': version_of([components['bun'], '--version']),
            'backend': version_of([marker['backend'], '--version']),
        }

        doctor = json.loads(require_ok(run([str(bin_dir / 'tap'), 'doctor'], env), 'doctor').stdout)
        report['doctor_after_install'] = {
            'healthy': doctor.get('healthy'),
            'components': doctor.get('components', {}).get('healthy'),
            'bridge': doctor.get('bridge', {}).get('healthy'),
        }
        if not doctor.get('healthy'):
            raise SystemExit('doctor not healthy after install: ' + json.dumps(doctor, indent=2))

        if not all(str(path).startswith(str(install_root / 'checkout'))
                   for path in components['readers']['projection']['command'][1:]):
            raise SystemExit('reader script is outside installed checkout')
        if report['runtime_modes']['TAP_BUN'] == 'download':
            if not str(components['bun']).startswith(str(install_root / 'bun')):
                raise SystemExit('downloaded Bun path is outside install root')
            report['bun_download_verified'] = True

        require_ok(run([str(bin_dir / 'tap'), 'off'], env), 'off')
        require_ok(run([str(bin_dir / 'tap'), 'on'], env), 'on')
        doctor2 = json.loads(require_ok(run([str(bin_dir / 'tap'), 'doctor'], env), 'doctor').stdout)
        report['doctor_after_restart'] = {
            'healthy': doctor2.get('healthy'),
            'components': doctor2.get('components', {}).get('healthy'),
        }
        if not doctor2.get('healthy'):
            raise SystemExit('doctor not healthy after restart')

        smoke = require_ok(run([components['bun'], str(install_root / 'checkout/instll/smoke_hub.mjs'),
                                str(install_root / 'profile')], env), 'hub smoke')
        report['hub_smoke'] = json.loads(smoke.stdout.strip().splitlines()[-1])

        verified, error = attempt_purge(install_root, env)
        report['cleanup_verified'] = verified
        if not verified:
            raise SystemExit('purge did not verify cleanup: ' + str(error))
        report['purged'] = True
        report['ok'] = True
    except KeyboardInterrupt:
        report['error'] = 'interrupted'
    except SystemExit as error:
        report['error'] = str(error)
    except subprocess.TimeoutExpired as error:
        report['error'] = f'timeout: {error}'
    except Exception as error:
        report['error'] = type(error).__name__ + ': ' + str(error)
    finally:
        try:
            if install_root.exists():
                verified, error = attempt_purge(install_root, env)
                report['cleanup_verified'] = verified
                if not verified:
                    report['cleanup_error'] = error
                    report['retained_root'] = str(install_root)
                    report['recovery'] = recovery_command(install_root)
                elif parent.exists():
                    shutil.rmtree(parent, ignore_errors=True)
            elif report.get('cleanup_verified') and parent.exists():
                shutil.rmtree(parent, ignore_errors=True)
        except KeyboardInterrupt as error:
            interrupted = error
            report['cleanup_verified'] = False
            report['cleanup_error'] = 'cleanup interrupted'
            report['retained_root'] = str(install_root)
            report['recovery'] = recovery_command(install_root)
        except Exception as error:
            # TimeoutExpired/OSError and any other cleanup failure must not skip emit.
            report['cleanup_verified'] = False
            report['cleanup_error'] = type(error).__name__ + ': ' + str(error)
            report['retained_root'] = str(install_root)
            report['recovery'] = recovery_command(install_root)

    emit(report, args.output)
    if interrupted is not None:
        raise interrupted
    return 0 if report.get('ok') and report.get('cleanup_verified') else 1


if __name__ == '__main__':
    raise SystemExit(main())
