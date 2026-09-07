#!/usr/bin/env python3
"""Hermetic Core update A→B check (#7) with #6 proxy policy assertions.

Installs from local archive A, seeds retained profile data + CA grant, updates
to archive B, and verifies routing/CA grants/data survive. Not Local Capture,
clean-Mac, or browser HTTPS-without-k.
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
sys.path.insert(0, str(ROOT))
from tap_core.runtime import Profile

spec_path = ROOT / 'instll/ownership.py'
import importlib.util
spec = importlib.util.spec_from_file_location('ownership', spec_path)
ownership = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ownership)


def pack(archive, marker):
    staging = archive.parent / ('tree-' + marker)
    if staging.exists():
        shutil.rmtree(staging)
    for name in ('tap', 'tap_core', 'instll', 'fixtures'):
        source = ROOT / name
        target = staging / name
        if source.is_dir():
            shutil.copytree(source, target)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
    (staging / 'UPDATE_MARKER').write_text(marker + '\n')
    with tarfile.open(archive, 'w:gz') as out:
        out.add(staging, arcname='tap-core-' + marker)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='tap-update-') as directory:
        parent = Path(directory).resolve()
        install_root = (parent / 'root').resolve()
        wrapper_dir = parent / 'bin'
        wrapper_dir.mkdir()
        archive_a = parent / 'a.tar.gz'
        archive_b = parent / 'b.tar.gz'
        pack(archive_a, 'version-a')
        pack(archive_b, 'version-b')
        fake_bin = parent / 'download-bin'
        fake_bin.mkdir()
        curl = fake_bin / 'curl'
        curl.write_text(
            '#!/bin/bash\n'
            'case "$*" in *api.github.com*) printf %s ' + 'a' * 40 + '; exit 0;; esac\n'
            'exec /bin/cat ' + shlex.quote(str(archive_a)) + '\n')
        curl.chmod(0o700)
        backend = parent / 'backend'
        backend.write_text('#!/bin/sh\necho "Mitmproxy: 12.2.3"\n')
        backend.chmod(0o700)
        bun = parent / 'bun'
        bun.write_text('#!/bin/sh\necho 1.3.11\n')
        bun.chmod(0o700)
        env = {**os.environ, 'PATH': str(fake_bin) + os.pathsep + os.environ['PATH'],
               'TAP_ROOT': str(install_root), 'TAP_BIN_DIR': str(wrapper_dir),
               'TAP_PYTHON': sys.executable, 'TAP_BACKEND': str(backend), 'TAP_BUN': str(bun),
               'TAP_SKIP_START': '1', 'TAP_ROUTING': 'explicit'}
        installed = subprocess.run(['/bin/bash', str(ROOT / 'instll/install')],
                                   env=env, capture_output=True, text=True)
        if installed.returncode:
            raise SystemExit(installed.stderr)
        Profile(install_root / 'profile', str(backend), 18999, 'explicit',
                'http://fixture.test', []).save()
        retained = install_root / 'profile/data/retained.txt'
        retained.parent.mkdir(parents=True, exist_ok=True)
        retained.write_text('keep-me\n')
        ca = install_root / 'profile/certificates/mitmproxy-ca-cert.pem'
        ca.parent.mkdir(parents=True, exist_ok=True)
        ca.write_text('-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n')
        ownership.grant_ca(str(install_root), str(ca), 'abcd' * 16)
        before = json.loads((install_root / 'install.json').read_text())
        updated = subprocess.run(['/bin/bash', str(install_root / 'checkout/instll/update')],
                                 env={**env, 'TAP_CHECKOUT_ARCHIVE': str(archive_b),
                                      'TAP_REF': 'b' * 40},
                                 capture_output=True, text=True)
        if updated.returncode:
            raise SystemExit(updated.stderr + updated.stdout)
        after = json.loads((install_root / 'install.json').read_text())
        assert after['ref'] == 'b' * 40
        assert after['routing'] == before['routing'] == 'explicit'
        assert after['grants']['ca']['sha256'] == before['grants']['ca']['sha256']
        assert retained.read_text() == 'keep-me\n'
        assert (install_root / 'checkout/UPDATE_MARKER').read_text().strip() == 'version-b'
        report = {
            'scope': 'hermetic local-archive update A→B; explicit routing; no network mutation, Local Capture, or browser HTTPS',
            'commit': subprocess.run(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'],
                                     text=True, capture_output=True).stdout.strip() or None,
            'update': {'ref_before': before['ref'], 'ref_after': after['ref'],
                       'checkout_marker': 'version-b'},
            'policy_proxy_hash6': {
                'routing_unchanged': after['routing'] == before['routing'],
                'routing': after['routing'],
                'ca_grant_unchanged': after['grants']['ca']['sha256'] == before['grants']['ca']['sha256'],
                'profile_data_retained': True,
                'local_capture': False,
            },
            'environment': {
                'python': sys.version.split()[0],
                'platform': platform.platform(),
                'machine': platform.machine(),
                'mac_ver': platform.mac_ver()[0] or None,
            },
            'wrapper_sha_matches': hashlib.sha256((wrapper_dir / 'tap').read_bytes()).hexdigest()
            == after['wrapper_sha256'],
        }
        rendered = json.dumps(report, indent=2) + '\n'
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered)
        print(rendered)


if __name__ == '__main__':
    main()
