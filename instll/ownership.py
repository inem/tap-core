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


def remove(root, purge):
    root = canonical(root)
    require(root not in (Path('/'), Path.home().resolve()), 'Refusing broad install root')
    require(purge in ('0', '1'), 'TAP_PURGE must be 0 or 1')
    marker = canonical(root / 'install.json')
    data = json.loads(marker.read_text())
    require(data.get('version') == 1 and data.get('root') == str(root), 'Ownership record does not match install root')
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
    # Use the same profile + network locks and recovery-first lifecycle as CLI.
    # Retain them through removal so concurrent on cannot restart between off
    # and deletion. No new independent process manager is introduced here.
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
                check_wrapper()
                wrapper.unlink(missing_ok=True)
                if purge == '1':
                    shutil.rmtree(root)
                print('Profile service removed; ' + ('installation purged' if purge == '1' else 'data and runtime retained'))


def main():
    try:
        if sys.argv[1] == 'record':
            record(sys.argv[2:])
        elif sys.argv[1] == 'remove':
            remove(*sys.argv[2:])
        else:
            raise ValueError('Unknown installer action')
    except Exception as error:
        print('tap-core: ' + str(error) + '; installation retained for recovery', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
