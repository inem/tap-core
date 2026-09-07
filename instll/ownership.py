"""Ownership and removal for the first installer; no runtime update protocol."""
from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import sys


def require(ok, message):
    if not ok:
        raise ValueError(message)


def canonical(path):
    path = Path(path).absolute()
    require(path == path.resolve(), 'Symlinked/noncanonical install path: ' + str(path))
    return path


def record(args):
    root, wrapper, python, backend, repo, ref, arch, port, routing, skip = args
    root, wrapper = Path(root).resolve(), Path(wrapper).parent.resolve() / Path(wrapper).name
    python = str(Path(python).absolute())
    require('\n' not in python, 'Interpreter path cannot contain a newline')
    require(not wrapper.exists() and not wrapper.is_symlink(), 'Command path is already occupied')
    require(not (root / 'install.json').exists(), 'Install ownership record already exists')
    code = '#!/bin/bash\nexec ' + shlex.join([python, str(root / 'checkout/tap'), '--profile', str(root / 'profile')]) + ' "$@"\n'
    data = {'version': 1, 'root': str(root), 'wrapper': str(wrapper), 'python': python,
            'backend': backend, 'repo': repo, 'ref': ref, 'arch': arch, 'port': int(port),
            'routing': routing, 'profile_requested': skip != '1',
            'wrapper_sha256': hashlib.sha256(code.encode()).hexdigest()}
    # Keep the recovery entrypoint before attempting to publish the command.
    with (root / 'interpreter').open('x') as handle:
        handle.write(python + '\n')
    with (root / 'install.json').open('x') as handle:
        json.dump(data, handle, indent=2)
        handle.write('\n')
    # O_EXCL refuses existing files and dangling symlinks, including a race
    # after the shell preflight. Never follow a foreign tap symlink.
    fd = os.open(wrapper, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
    with os.fdopen(fd, 'w') as handle:
        handle.write(code)


def _load_mark(root):
    marker = canonical(root / 'install.json')
    data = json.loads(marker.read_text())
    require(data.get('version') == 1 and data.get('root') == str(root),
            'Ownership record does not match install root')
    return marker, data


def _save_mark(marker, data):
    temporary = marker.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, indent=2) + '\n')
    temporary.chmod(0o600)
    temporary.replace(marker)


def grant_sudoers(root, path, digest, alias):
    root = canonical(root)
    marker, data = _load_mark(root)
    grants = dict(data.get('grants') or {})
    grants['sudoers'] = {'path': str(path), 'sha256': digest, 'alias': alias}
    data['grants'] = grants
    _save_mark(marker, data)


def grant_ca(root, cert, digest):
    root = canonical(root)
    marker, data = _load_mark(root)
    grants = dict(data.get('grants') or {})
    grants['ca'] = {'cert': str(Path(cert).resolve()), 'sha256': digest.lower()}
    data['grants'] = grants
    _save_mark(marker, data)


def cert_fingerprint(path):
    """SHA-256 fingerprint of the certificate DER (openssl-compatible, lowercase hex)."""
    import base64
    import re
    text = Path(path).read_text()
    match = re.search(r'-----BEGIN CERTIFICATE-----(.*?)-----END CERTIFICATE-----', text, re.S)
    require(match, 'CA path is not a PEM certificate: ' + str(path))
    der = base64.b64decode(''.join(match.group(1).split()))
    return hashlib.sha256(der).hexdigest()


def revoke_grants(root, data, runner=None):
    """Remove grants owned by this install. Failures stay explicit; never silent."""
    import subprocess
    run = runner or (lambda command: subprocess.run(command, text=True, capture_output=True, check=False))
    grants = data.get('grants') or {}
    errors = []
    sudoers = grants.get('sudoers') or {}
    path = sudoers.get('path')
    if path:
        target = Path(path)
        try:
            if target.is_symlink():
                errors.append('sudoers path is a symlink; left untouched: ' + str(target))
            elif target.is_file():
                listed = run(['/usr/bin/sudo', '-n', 'cat', str(target)])
                if listed.returncode != 0:
                    errors.append('sudoers read failed (need interactive sudo -v then retry): '
                                  + (listed.stderr or listed.stdout).strip())
                else:
                    owned = f'tap-core-owned root={root}' in listed.stdout
                    digest = hashlib.sha256(listed.stdout.encode()).hexdigest()
                    if owned and digest == sudoers.get('sha256'):
                        removed = run(['/usr/bin/sudo', '-n', 'rm', '-f', str(target)])
                        if removed.returncode != 0:
                            errors.append('sudoers remove failed: '
                                          + (removed.stderr or removed.stdout).strip())
                    else:
                        errors.append('sudoers file present but not owned/matched; left untouched: '
                                      + str(target))
        except OSError as error:
            errors.append('sudoers revoke error: ' + str(error))
    ca = grants.get('ca') or {}
    if ca:
        cert = ca.get('cert')
        expected = (ca.get('sha256') or '').lower()
        if not cert or not Path(cert).is_file():
            errors.append('CA cert missing at recorded path; System keychain trust may remain. '
                          'Restore the cert file or remove that trust manually, then retry uninstall')
        else:
            try:
                actual = cert_fingerprint(cert)
                if actual != expected:
                    errors.append('CA fingerprint mismatch (recorded=%s actual=%s); '
                                  'refusing to revoke possibly foreign trust' % (expected, actual))
                else:
                    removed = run(['/usr/bin/sudo', '-n', '/usr/bin/security',
                                   'remove-trusted-cert', '-d', cert])
                    if removed.returncode != 0:
                        errors.append('CA trust revoke failed (sudo -v then retry): '
                                      + (removed.stderr or removed.stdout).strip())
            except (OSError, ValueError) as error:
                errors.append('CA revoke error: ' + str(error))
    return errors


def remove(root, purge):
    root = canonical(root)
    require(root not in (Path('/'), Path.home().resolve()), 'Refusing broad install root')
    require(purge in ('0', '1'), 'TAP_PURGE must be 0 or 1')
    marker, data = _load_mark(root)
    require(type(data.get('profile_requested')) is bool, 'Invalid install state')
    require(str(Path(__file__).resolve()) == str(root / 'checkout/instll/ownership.py'),
            'Use the uninstaller from this installation')
    wrapper = canonical(data['wrapper'])
    def check_wrapper():
        canonical(wrapper)
        if wrapper.exists():
            require(wrapper.is_file() and hashlib.sha256(wrapper.read_bytes()).hexdigest() == data['wrapper_sha256'],
                    'Command was replaced by another owner; refusing removal')
    check_wrapper()
    profile_root = canonical(root / 'profile')
    sys.path.insert(0, str(root / 'checkout'))
    from tap_core.runtime import MacOS, Profile, profile_lock
    from tap_core.routing import select_routing
    from tap_core.cli import mutate
    with profile_lock(root):
        configured = (profile_root / 'profile.json').is_file()
        require(configured or not data['profile_requested'],
                'Requested profile configuration is missing; cannot prove service cleanup')
        require(configured or not profile_root.exists(), 'Incomplete profile; inspect before removal')
        with profile_lock(profile_root) if configured else nullcontext():
            if configured:
                profile = Profile.load(profile_root)
                adapter = MacOS()
                network_lock = select_routing(profile, adapter).mutation_lock()
            else:
                network_lock = nullcontext()
            with network_lock:
                if configured:
                    mutate('uninstall', profile, adapter)
                grant_errors = revoke_grants(root, data)
                check_wrapper()
                if grant_errors:
                    raise ValueError(
                        'grant cleanup incomplete: ' + '; '.join(grant_errors)
                        + '; run: sudo -v && TAP_ROOT=' + str(root)
                        + ' bash ' + str(root / 'checkout/instll/uninstall')
                        + ' — marker/CA/runtime retained')
                wrapper.unlink(missing_ok=True)
                if purge == '1':
                    shutil.rmtree(root)
                print('Profile service removed; ' + (
                    'installation purged' if purge == '1' else 'data and runtime retained'))



def main():
    try:
        if sys.argv[1] == 'record':
            record(sys.argv[2:])
        elif sys.argv[1] == 'remove':
            remove(*sys.argv[2:])
        elif sys.argv[1] == 'grant-sudoers':
            grant_sudoers(*sys.argv[2:])
        elif sys.argv[1] == 'grant-ca':
            grant_ca(*sys.argv[2:])
        else:
            raise ValueError('Unknown installer action')
    except Exception as error:
        print('tap-core: ' + str(error) + '; installation retained for recovery', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
