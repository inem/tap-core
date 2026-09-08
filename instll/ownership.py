"""Ownership, removal and in-place update for the first installer."""
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



def wrapper_code(python, root):
    return ('#!/bin/bash\nexec '
            + shlex.join([python, str(Path(root) / 'checkout/tap'), '--profile', str(Path(root) / 'profile')])
            + ' "$@"\n')


def record(args):
    root, wrapper, python, backend, repo, ref, arch, port, routing, skip = args
    root, wrapper = Path(root).resolve(), Path(wrapper).parent.resolve() / Path(wrapper).name
    python = str(Path(python).absolute())
    require('\n' not in python, 'Interpreter path cannot contain a newline')
    require(not wrapper.exists() and not wrapper.is_symlink(), 'Command path is already occupied')
    require(not (root / 'install.json').exists(), 'Install ownership record already exists')
    code = wrapper_code(python, root)
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



def verify_owned(root):
    """Confirm install.json + wrapper still belong to this root."""
    root = canonical(root)
    marker, data = _load_mark(root)
    wrapper = Path(data['wrapper'])
    require(not wrapper.is_symlink(), 'Command path is a symlink; refusing update')
    require(wrapper.is_file(), 'Owned command is missing; refusing update')
    require(hashlib.sha256(wrapper.read_bytes()).hexdigest() == data['wrapper_sha256'],
            'Command was replaced by another owner; refusing update')
    require(data.get('routing') in ('explicit', 'system'), 'Invalid routing in ownership record')
    return data


def refresh(root, python, backend, ref, arch):
    """Rewrite wrapper/mark; roll both back if either step fails."""
    root = canonical(root)
    marker, data = _load_mark(root)
    verify_owned(root)
    python = str(Path(python).absolute())
    require('\n' not in python, 'Interpreter path cannot contain a newline')
    require(Path(python).is_file(), 'Updated interpreter is missing')
    code = wrapper_code(python, root)
    wrapper = Path(data['wrapper'])
    previous_mark = marker.read_bytes()
    previous_wrapper = wrapper.read_bytes()
    previous_interpreter = (root / 'interpreter').read_bytes()
    updated = dict(data, python=python, backend=backend, ref=ref, arch=arch,
                   wrapper_sha256=hashlib.sha256(code.encode()).hexdigest())
    temporary = wrapper.with_name(wrapper.name + '.tmp')
    try:
        temporary.write_text(code)
        temporary.chmod(0o700)
        temporary.replace(wrapper)
        _save_mark(marker, updated)
        (root / 'interpreter').write_text(python + '\n')
    except Exception:
        temporary.unlink(missing_ok=True)
        wrapper.write_bytes(previous_wrapper)
        marker.write_bytes(previous_mark)
        (root / 'interpreter').write_bytes(previous_interpreter)
        raise
    return updated


def assert_idle(root):
    """Fail if install root or profile lock is already held (different process)."""
    root = canonical(root)
    verify_owned(root)
    sys.path.insert(0, str(root / 'checkout'))
    from tap_core.runtime import profile_lock
    profile_root = root / 'profile'
    with profile_lock(root, busy_message='Another install/update holds this root'):
        if (profile_root / 'profile.json').is_file():
            with profile_lock(profile_root, busy_message='Another command is changing this profile'):
                pass
    return {'ok': True, 'root': str(root)}


def _component_job(profile):
    if profile.components is None:
        return None
    from tap_core.components import Job
    return Job(profile)


def _profile_processes_busy(adapter, profile):
    """Proxy launchd/port or components controller (and Hub port when required)."""
    busy = []
    if adapter.service_loaded(profile):
        busy.append('proxy service')
    if adapter.port_open(profile):
        busy.append('proxy port')
    job = _component_job(profile)
    if job is not None:
        if adapter.service_loaded(job):
            busy.append('components service')
        from tap_core.components import needs_hub
        from tap_core.pack_store import PackStore
        effective = PackStore(profile.root).effective_components(profile.components)
        if needs_hub(effective, profile.bridge) and adapter.port_open(job):
            busy.append('hub port')
    return busy


def _ensure_profile_stopped(profile_root):
    """Refuse update unless proxy, Hub/readers and network recovery are clear."""
    from tap_core.runtime import MacOS, Profile, TapError
    from tap_core.routing import select_routing
    from tap_core.cli import mutate
    try:
        profile = Profile.load(profile_root)
        adapter = MacOS()
        route = select_routing(profile, adapter)
    except (TapError, OSError, ValueError) as error:
        raise ValueError('Cannot inspect profile service state; refusing update: ' + str(error)) from error
    try:
        busy = _profile_processes_busy(adapter, profile)
        pending = route.recovery_pending()
    except (TapError, OSError) as error:
        raise ValueError('Process state is unknown; refusing update: ' + str(error)) from error
    if not busy and not pending:
        return False
    try:
        with route.mutation_lock():
            mutate('off', profile, adapter)
    except (TapError, OSError, ValueError) as error:
        raise ValueError('Cannot stop profile before update: ' + str(error)) from error
    try:
        busy = _profile_processes_busy(adapter, profile)
        pending = route.recovery_pending()
    except (TapError, OSError) as error:
        raise ValueError('Process state is unknown after stop; refusing update: ' + str(error)) from error
    if busy:
        raise ValueError('Profile still running after stop (' + ', '.join(busy) + '); refusing update')
    if pending:
        raise ValueError('Network recovery still pending after stop; refusing update')
    return True


def _map_runtime_arg(arg, old_python, old_bun, python, bun):
    if arg == old_python:
        return python
    if arg == old_bun:
        return bun
    return arg


def _propagate_profile(root, profile_root, backend, python, bun):
    """Point profile.backend (and component runtime paths) at the new install.

    Preserves port, hub_port, exclude/allow origins, and user-defined readers/
    handlers; only rewrites known runtime path bindings.
    """
    from tap_core.runtime import Profile
    profile = Profile.load(profile_root)
    profile.backend = backend
    if profile.components is not None:
        old_python = profile.components.get('python')
        old_bun = profile.components.get('bun')
        components = json.loads(json.dumps(profile.components))
        components['python'] = python
        components['bun'] = bun
        for group in ('readers', 'handlers'):
            for spec in components.get(group, {}).values():
                command = spec.get('command')
                if isinstance(command, list):
                    spec['command'] = [
                        _map_runtime_arg(arg, old_python, old_bun, python, bun) for arg in command
                    ]
        profile.components = components
    profile.save()


def _promote_staged(staged, destination):
    """Move staged path into destination; return previous path (kept until success)."""
    staged, destination = Path(staged), Path(destination)
    require(staged.exists(), 'Staged runtime missing: ' + str(staged))
    previous = destination.with_name(destination.name + '.prev.' + str(os.getpid()))
    if previous.exists():
        shutil.rmtree(previous) if previous.is_dir() else previous.unlink()
    kept = None
    if destination.exists():
        destination.rename(previous)
        kept = previous
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        staged.rename(destination)
    except Exception:
        if kept is not None and not destination.exists():
            kept.rename(destination)
        raise
    return kept


def _drop_path(path):
    if path is None:
        return
    path = Path(path)
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    elif path.exists():
        path.unlink(missing_ok=True)


def _restore_path(previous, destination):
    """Restore a replaced runtime. No-op when that runtime was never staged."""
    if previous is None:
        return
    destination = Path(destination)
    if not Path(previous).exists():
        require(destination.exists(), 'Runtime and its backup are both missing: ' + str(destination))
        return  # An earlier rollback attempt already moved this backup back.
    if destination.exists():
        shutil.rmtree(destination) if destination.is_dir() else destination.unlink(missing_ok=True)
    if Path(previous).exists():
        Path(previous).rename(destination)


def _pending_path(root):
    return Path(root) / 'update-pending.json'


def _save_pending(root, pending):
    path = _pending_path(root)
    temporary = path.with_suffix('.tmp')
    try:
        temporary.write_text(json.dumps(pending, indent=2) + '\n')
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _prepare_recovery(root, checkout, python):
    """Keep B's recovery code available even after restoring a pre-update A."""
    recovery = root / 'update-recovery'
    require(not recovery.exists(), 'Leftover update-recovery; inspect before update')
    recovery.mkdir(mode=0o700)
    try:
        shutil.copyfile(checkout / 'instll/ownership.py', recovery / 'ownership.py')
        shutil.copytree(checkout / 'tap_core', recovery / 'tap_core',
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        # A's interpreter path exists before promotion and again after restoration.
        # Do not retain a staging path that the updater's EXIT trap will remove.
        command = shlex.join([python, str(recovery / 'ownership.py'),
                              'rollback-update', str(root)])
        (recovery / 'rollback').write_text('#!/bin/bash\nexec ' + command + '\n')
    except Exception:
        _drop_path(recovery)
        raise


def _with_install_locks(root, profile_root, configured, body):
    """Serialize terminal update transitions with the same locks as apply."""
    helper_root = Path(__file__).resolve().parent
    sys.path.insert(0, str(helper_root if (helper_root / 'tap_core').is_dir()
                           else root / 'checkout'))
    from tap_core.runtime import profile_lock
    with profile_lock(root, busy_message='Another install/update holds this root'):
        with (profile_lock(profile_root, busy_message='Another command is changing this profile')
              if configured else nullcontext()):
            return body()


def finalize_update(root):
    """Drop checkout/runtime backups after B has started (or start was skipped)."""
    root = canonical(root)
    path = _pending_path(root)
    require(path.is_file(), 'No pending update to finalize')
    profile_root = root / 'profile'
    configured = (profile_root / 'profile.json').is_file()

    def body():
        pending = json.loads(path.read_text())
        for key in ('checkout_backup', 'managed_backup', 'previous_python', 'previous_backend',
                    'previous_bun', 'profile_backup'):
            value = pending.get(key) or ''
            if value:
                _drop_path(value)
        path.unlink(missing_ok=True)
        require(not list(root.glob('checkout.prev.*')), 'Checkout backup survived finalize')
        _drop_path(root / 'update-recovery')
        return {'ok': True, 'root': str(root)}

    return _with_install_locks(root, profile_root, configured, body)


def rollback_update(root):
    """Restore A after apply committed B but start/post-check failed.

    Holds root/profile locks, stops B (and clears recovery) before replacing
    files, and keeps update-pending.json until mark/wrapper refresh succeeds.
    """
    import subprocess
    root = canonical(root)
    path = _pending_path(root)
    require(path.is_file(), 'No pending update to roll back')
    profile_root = root / 'profile'
    # Prefer live profile.json; during retry after files_restored it is already A.
    configured = (profile_root / 'profile.json').is_file()

    def body():
        pending = json.loads(path.read_text())
        before = pending.get('before') or {}
        require(before.get('python') and before.get('backend') and before.get('ref') and before.get('arch'),
                'Pending update is missing ownership restore fields')
        checkout = root / 'checkout'
        backup = Path(pending.get('checkout_backup') or '')
        managed_dir = root / 'managed'
        managed_backup = Path(pending['managed_backup']) if pending.get('managed_backup') else None
        hub_port = int(pending.get('hub_port') or 0)
        phase = pending.get('phase') or 'applied'

        if phase == 'applied':
            # Stop B while its checkout is still current; refuse if unknown/busy.
            if configured:
                _ensure_profile_stopped(profile_root)
            if checkout.exists() and backup.exists():
                shutil.rmtree(checkout)
            if backup.exists() and not checkout.exists():
                backup.rename(checkout)
            if managed_dir.exists() and managed_backup is not None and managed_backup.exists():
                shutil.rmtree(managed_dir)
            if managed_backup is not None and managed_backup.exists() and not managed_dir.exists():
                managed_backup.rename(managed_dir)
            for key in ('created_python', 'created_backend', 'created_bun'):
                if pending.get(key):
                    _drop_path(pending[key])
            _restore_path(pending.get('previous_python') or None, root / 'python')
            _restore_path(pending.get('previous_backend') or None, root / 'backend/mitmproxy.app')
            _restore_path(pending.get('previous_bun') or None, root / 'bun/bin/bun')
            profile_backup = pending.get('profile_backup') or ''
            if profile_backup and Path(profile_backup).is_file():
                (profile_root / 'profile.json').write_bytes(Path(profile_backup).read_bytes())
            pending['phase'] = 'files_restored'
            _save_pending(root, pending)

        # Mark/wrapper must match A before pending is cleared.
        if not managed_dir.exists():
            helper = checkout / 'instll/write_managed.py'
            bun = before.get('bun') or ''
            require(helper.is_file() and bun and hub_port,
                    'Cannot rebuild managed bindings during rollback')
            result = subprocess.run(
                [before['python'], str(helper), str(root), str(checkout), before['python'], bun,
                 str(hub_port), str(profile_root)],
                capture_output=True, text=True)
            if result.returncode:
                raise ValueError('rollback write_managed failed: '
                                 + (result.stderr or result.stdout).strip())
        refresh(root, before['python'], before['backend'], before['ref'], before['arch'])
        profile_backup = pending.get('profile_backup') or ''
        if profile_backup:
            _drop_path(profile_backup)
        path.unlink(missing_ok=True)
        _drop_path(root / 'update-recovery')
        return {'ok': True, 'root': str(root), 'restored': True}

    return _with_install_locks(root, profile_root, configured, body)


def apply_checkout(root, new_checkout, python, backend, bun, ref, arch, hub_port,
                   staged_python='', staged_backend='', staged_bun=''):
    """Swap checkout under install+profile locks; keep A until finalize after start.

    Optional staged_* paths are prepared outside the install root and promoted only
    while locks are held, so update does not mutate the root past a held lock.
    On success leaves update-pending.json; caller must finalize-update after B starts
    (or immediately when start is skipped), or rollback-update if start fails.
    """
    import subprocess
    root = canonical(root)
    before = verify_owned(root)
    routing_before = before['routing']
    ca_before = ((before.get('grants') or {}).get('ca') or {}).get('sha256') or ''
    new_checkout = Path(new_checkout).resolve()
    require((new_checkout / 'tap').is_file(), 'New checkout missing tap entrypoint')
    require((new_checkout / 'instll/ownership.py').is_file(), 'New checkout missing ownership helper')
    require(str(hub_port).isdigit(), 'hub_port must be an integer')
    hub_port = int(hub_port)
    staged_python = staged_python or ''
    staged_backend = staged_backend or ''
    staged_bun = staged_bun or ''
    checkout = root / 'checkout'
    backup = root / ('checkout.prev.' + str(os.getpid()))
    profile_root = root / 'profile'
    require(not backup.exists(), 'Leftover checkout backup present; inspect before update')
    require(not _pending_path(root).exists(), 'Leftover update-pending.json; finalize or rollback first')
    bun_for_restore = bun
    managed_components = root / 'managed/components.json'
    if managed_components.is_file():
        bun_for_restore = json.loads(managed_components.read_text()).get('bun') or bun
    profile_backup = None
    managed_backup = None
    managed_dir = root / 'managed'
    if managed_dir.is_dir():
        managed_backup = root / ('managed.prev.' + str(os.getpid()))
        require(not managed_backup.exists(), 'Leftover managed backup present; inspect before update')

    sys.path.insert(0, str(checkout))
    from tap_core.runtime import profile_lock

    def write_managed(target_checkout, python_path, bun_path):
        helper = target_checkout / 'instll/write_managed.py'
        require(helper.is_file(), 'write_managed helper missing in checkout')
        result = subprocess.run(
            [python_path, str(helper), str(root), str(target_checkout), python_path, bun_path,
             str(hub_port), str(profile_root)],
            capture_output=True, text=True)
        if result.returncode:
            raise ValueError('write_managed failed: ' + (result.stderr or result.stdout).strip())

    was_running = False
    previous_python = previous_backend = previous_bun = None
    created_python = created_backend = created_bun = None
    with profile_lock(root, busy_message='Another install/update holds this root'):
        configured = (profile_root / 'profile.json').is_file()
        with (profile_lock(profile_root, busy_message='Another command is changing this profile')
              if configured else nullcontext()):
            if configured:
                was_running = _ensure_profile_stopped(profile_root)
                profile_backup = (profile_root / 'profile.json').read_bytes()
            _prepare_recovery(root, new_checkout, before['python'])
            try:
                if staged_python:
                    previous_python = _promote_staged(staged_python, root / 'python')
                    if previous_python is None:
                        created_python = root / 'python'
                    python = str(root / 'python/bin/python3')
                else:
                    python = str(Path(python).absolute())
                if staged_backend:
                    previous_backend = _promote_staged(staged_backend, root / 'backend/mitmproxy.app')
                    if previous_backend is None:
                        created_backend = root / 'backend/mitmproxy.app'
                    backend = str(root / 'backend/mitmproxy.app/Contents/MacOS/mitmdump')
                else:
                    backend = str(Path(backend).absolute())
                if staged_bun:
                    previous_bun = _promote_staged(staged_bun, root / 'bun/bin/bun')
                    if previous_bun is None:
                        created_bun = root / 'bun/bin/bun'
                    bun = str(root / 'bun/bin/bun')
                    Path(bun).chmod(0o700)
                else:
                    bun = str(Path(bun).absolute())
                require(Path(python).is_file(), 'Updated interpreter is missing')
                require(Path(bun).is_file(), 'Bun executable missing')
                require(Path(backend).exists(), 'Backend path missing')
                if managed_backup is not None and managed_dir.exists():
                    managed_dir.rename(managed_backup)
                checkout.rename(backup)
                try:
                    new_checkout.rename(checkout)
                except Exception:
                    if not checkout.exists() and backup.exists():
                        backup.rename(checkout)
                    if managed_backup is not None and managed_backup.exists() and not managed_dir.exists():
                        managed_backup.rename(managed_dir)
                    raise
                write_managed(checkout, python, bun)
                if configured:
                    _propagate_profile(root, profile_root, backend, python, bun)
                data = refresh(root, python, backend, ref, arch)
                require(data['routing'] == routing_before, 'routing changed during update')
                ca_after = ((data.get('grants') or {}).get('ca') or {}).get('sha256') or ''
                require(ca_after == ca_before, 'CA grant fingerprint changed during update')
                profile_prev = ''
                if profile_backup is not None:
                    profile_prev_path = profile_root / 'profile.json.prev'
                    profile_prev_path.write_bytes(profile_backup)
                    profile_prev = str(profile_prev_path)
                pending = {
                    'phase': 'applied',
                    'checkout_backup': str(backup),
                    'managed_backup': str(managed_backup) if managed_backup is not None and managed_backup.exists() else '',
                    'previous_python': str(previous_python) if previous_python else '',
                    'previous_backend': str(previous_backend) if previous_backend else '',
                    'previous_bun': str(previous_bun) if previous_bun else '',
                    'created_python': str(created_python) if created_python else '',
                    'created_backend': str(created_backend) if created_backend else '',
                    'created_bun': str(created_bun) if created_bun else '',
                    'profile_backup': profile_prev,
                    'hub_port': hub_port,
                    'before': {
                        'python': before['python'],
                        'backend': before['backend'],
                        'ref': before['ref'],
                        'arch': before['arch'],
                        'bun': bun_for_restore,
                    },
                }
                _save_pending(root, pending)
                # A stays until finalize-update after B starts successfully.
            except Exception:
                if checkout.exists() and backup.exists():
                    shutil.rmtree(checkout)
                if backup.exists() and not checkout.exists():
                    backup.rename(checkout)
                if managed_dir.exists() and managed_backup is not None and managed_backup.exists():
                    shutil.rmtree(managed_dir)
                if managed_backup is not None and managed_backup.exists() and not managed_dir.exists():
                    managed_backup.rename(managed_dir)
                _drop_path(created_python)
                _drop_path(created_backend)
                _drop_path(created_bun)
                _restore_path(previous_python, root / 'python')
                _restore_path(previous_backend, root / 'backend/mitmproxy.app')
                _restore_path(previous_bun, root / 'bun/bin/bun')
                if profile_backup is not None:
                    (profile_root / 'profile.json').write_bytes(profile_backup)
                if checkout.exists():
                    try:
                        if not managed_dir.exists():
                            write_managed(checkout, before['python'], bun_for_restore)
                    except Exception:
                        pass
                    try:
                        refresh(root, before['python'], before['backend'],
                                before['ref'], before['arch'])
                        _drop_path(root / 'update-recovery')
                    except Exception:
                        pass
                raise
            require(backup.exists(), 'Checkout backup missing after apply; cannot roll back a failed start')
            require(_pending_path(root).is_file(), 'update-pending.json missing after apply')
    data['was_running'] = was_running
    data['pending'] = True
    return data


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
        elif sys.argv[1] == 'verify':
            verify_owned(sys.argv[2])
            print(json.dumps({'ok': True, 'root': sys.argv[2]}))
        elif sys.argv[1] == 'assert-idle':
            print(json.dumps(assert_idle(sys.argv[2])))
        elif sys.argv[1] == 'refresh':
            data = refresh(*sys.argv[2:7])
            print(json.dumps({'ok': True, 'ref': data['ref'], 'routing': data['routing'],
                              'grants': data.get('grants') or {}}))
        elif sys.argv[1] == 'apply-checkout':
            # Optional trailing staged runtime paths (empty string = already installed).
            args = sys.argv[2:13]
            while len(args) < 11:
                args.append('')
            data = apply_checkout(*args[:11])
            print(json.dumps({'ok': True, 'ref': data['ref'], 'routing': data['routing'],
                              'grants': data.get('grants') or {}, 'backup_retained': True,
                              'pending': True, 'was_running': data.get('was_running')}))
        elif sys.argv[1] == 'finalize-update':
            print(json.dumps(finalize_update(sys.argv[2])))
        elif sys.argv[1] == 'rollback-update':
            print(json.dumps(rollback_update(sys.argv[2])))
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
