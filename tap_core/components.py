"""Explicit development bindings and launchd lifecycle for owned components."""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import plistlib
import secrets
from urllib.request import ProxyHandler, Request, build_opener

from .runtime import TapError, StartupError, atomic_json
from .bridge import exact_origin, fingerprint, read_token
from .readers import Reader, validate_definition

ROOT = Path(__file__).resolve().parent
BUN_VERSION = '1.3.11'


def needs_hub(components, bridge=None):
    """Hub/Bun only when a page handler needs the request/result transport."""
    return type(components) is dict and bool(components.get('handlers'))


def configuration(value, profile):
    if (type(value) is not dict or set(value) != {'version', 'python', 'bun', 'readers', 'handlers'}
            or type(value['version']) is not int or value['version'] != 1):
        raise TapError('Components require version 1, python, bun, readers and handlers')
    if profile.bridge is None:
        raise TapError('Components require a bridge configuration; use enabled=false for reader-only')
    if not isinstance(value['python'], str) or not Path(value['python']).is_absolute():
        raise TapError('Component python requires an explicit absolute path')
    if not isinstance(value['bun'], str):
        raise TapError('Component bun path must be a string')
    if value.get('handlers') and not profile.bridge['enabled']:
        raise TapError('Handlers require an enabled bridge')
    if needs_hub(value, profile.bridge):
        if not Path(value['bun']).is_absolute():
            raise TapError('Component bun requires an explicit absolute path when Hub is required')
    for name in ('readers', 'handlers'):
        if type(value[name]) is not dict or len(value[name]) > 8:
            raise TapError('At most eight named readers/handlers per development profile')
    for name, spec in value['readers'].items():
        Reader(profile, name)
        validate_definition(spec)
    for name, spec in value['handlers'].items():
        Reader(profile, name)  # Same bounded identifier syntax, separate state root.
        if type(spec) is not dict or set(spec) != {'command', 'config', 'origins'}:
            raise TapError('Handler requires command, config and explicit origins')
        validate_definition(dict(version=1, revision='handler-1', command=spec['command'], config=spec['config']))
        if type(spec['origins']) is not list or len(spec['origins']) > 64:
            raise TapError('Handler origins must be a bounded list')
        for origin in spec['origins']:
            exact_origin(origin)
    return value


def secret(profile):
    try:
        return read_token(profile.root, 'component-token')
    except ValueError as error:
        raise TapError(str(error)) from error


def runtime_bridge(bridge, profile):
    """Project only the bridge fields consumed by managed components and Hub."""
    return {
        'enabled': bridge['enabled'],
        'hub_port': bridge['hub_port'],
        'allow_origins': list(bridge['allow_origins']),
        'exclude_origins': list(bridge.get('exclude_origins') or profile.bridge.get('exclude_origins') or []),
    }


def prepare(profile):
    configuration(profile.components, profile)
    from .readers import private_dir
    from .pack_store import PackStore
    private_dir(profile.root / 'state')
    path = profile.root / 'state/component-token'
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        secret(profile)
    else:
        with os.fdopen(fd, 'w') as handle:
            handle.write(secrets.token_hex(24) + '\n')
    store = PackStore(profile.root)
    effective_bridge = store.effective_bridge(profile.bridge) or profile.bridge
    effective_components = store.effective_components(profile.components)
    configuration(effective_components, profile)
    atomic_json(profile.root / 'state/effective-runtime.json', {
        'bridge': runtime_bridge(effective_bridge, profile),
        'components': effective_components,
    })
    for name in effective_components['handlers']:
        for parent in ('state', 'data', 'logs'):
            private_dir(profile.root / parent / 'handlers')
            private_dir(profile.root / parent / 'handlers' / name)
    return effective_components


@dataclass
class Job:
    profile: object
    @property
    def label(self):
        return self.profile.label + '.components'
    @property
    def plist(self):
        return self.profile.plist.with_name(self.label + '.plist')
    @property
    def port(self):
        return self.profile.bridge['hub_port']


def identity(profile):
    from .pack_store import PackStore
    store = PackStore(profile.root)
    effective_bridge = store.effective_bridge(profile.bridge) or profile.bridge
    effective_components = store.effective_components(profile.components)
    return fingerprint({'components': effective_components,
                        'bridge': runtime_bridge(effective_bridge, profile)})


def hub_health(profile):
    request = Request('http://127.0.0.1:%s/health' % profile.bridge['hub_port'],
                      headers={'Authorization': 'Bearer ' + secret(profile)})
    with build_opener(ProxyHandler({})).open(request, timeout=1) as response:
        return json.loads(response.read(16384))


def status(profile, adapter):
    if profile.components is None:
        return {'configured': False, 'healthy': True}
    import math
    import time
    job = Job(profile)
    pid = adapter.service_pid(job)
    try:
        state = json.loads((profile.root / 'state/components.json').read_text())
    except FileNotFoundError:
        return {'configured': True, 'healthy': False, 'pid': pid, 'phase': 'absent'}
    if (type(state) is not dict or type(state.get('pid')) is not int
            or type(state.get('updated_at')) not in (int, float)
            or not math.isfinite(state['updated_at']) or type(state.get('healthy')) is not bool):
        raise TapError('Malformed component observation')
    fresh = time.time() - 5 < state['updated_at'] <= time.time() + 1
    current = pid == state['pid'] and state.get('configuration') == identity(profile)
    # Infrastructure readiness and aggregate component health are deliberately
    # separate. A reader can preserve an explicit failure (for example a
    # retention gap) while the owned controller and Hub remain able to serve
    # current capture/page traffic.
    live = False
    if current and fresh and state.get('phase') == 'ready':
        hub_pid = state.get('hub_pid')
        if hub_pid is None:
            # Reader-only controller: no Hub process to probe.
            live = True
        else:
            try:
                live = hub_health(profile).get('pid') == hub_pid
            except OSError:
                pass
    ready = bool(current and fresh and live)
    return dict(state, configured=True, current_process=current, ready=ready,
                healthy=bool(ready and state['healthy']))


def start(profile, adapter):
    try:
        _start(profile, adapter)
    except (OSError, TapError, ValueError) as error:
        # Proxy startup precedes components. Any component failure must unwind
        # the jobs started by this lifecycle command, including preflight errors.
        raise StartupError(str(error)) from error


def _start(profile, adapter):
    if profile.components is None:
        return
    job = Job(profile)
    if adapter.service_pid(job):
        if status(profile, adapter).get('ready'):
            return
        raise StartupError('Profile component infrastructure is not ready; inspect status/logs and use off/on')
    # Pack enable/disable/update only touch the registry; refresh Hub/reader snapshot here.
    config = prepare(profile)
    hubful = needs_hub(config, profile.bridge)
    if hubful and adapter.port_open(job):
        raise StartupError('Component port is occupied; its owner will not be stopped')
    if adapter.service_loaded(job):
        stop(profile, adapter)
    if not Path(config['python']).is_file() or not os.access(config['python'], os.X_OK):
        raise StartupError('Component runtime is not executable: ' + config['python'])
    if hubful:
        if not Path(config['bun']).is_file() or not os.access(config['bun'], os.X_OK):
            raise StartupError('Component runtime is not executable: ' + config['bun'])
        if adapter.run([config['bun'], '--version']).stdout.strip() != BUN_VERSION:
            raise StartupError('This development slice requires Bun ' + BUN_VERSION)
    adapter.run([config['python'], '-c', 'import sys; assert sys.version_info >= (3, 9)'])
    # Explicit manual start resets the bounded crash-restart budget.
    (profile.root / 'state/component-starts.json').unlink(missing_ok=True)
    plist = {'Label': job.label, 'ProgramArguments': [config['python'], '-B', str(ROOT / 'service.py'), str(profile.root)],
             'WorkingDirectory': str(profile.root), 'RunAtLoad': True,
             'KeepAlive': {'SuccessfulExit': False}, 'ThrottleInterval': 3,
             'ExitTimeOut': 10, 'AbandonProcessGroup': False,
             'StandardOutPath': str(profile.root / 'logs/components.log'),
             'StandardErrorPath': str(profile.root / 'logs/components.log')}
    job.plist.write_bytes(plistlib.dumps(plist))
    job.plist.chmod(0o600)
    try:
        adapter.run(['/bin/launchctl', 'bootstrap', 'gui/' + str(os.getuid()), job.plist])
        if not adapter.wait(lambda: status(profile, adapter).get('ready', False), seconds=15):
            raise TapError('Component infrastructure did not become ready; see components.log')
    except (OSError, TapError, ValueError) as error:
        raise StartupError(str(error)) from error


def stop(profile, adapter):
    if profile.components is None:
        return
    job = Job(profile)
    owned = adapter.service_loaded(job)
    from .pack_store import PackStore
    hubful = needs_hub(PackStore(profile.root).effective_components(profile.components),
                       profile.bridge)
    # Existing adapter removes only this exact job and waits for its leader.
    adapter.stop(job)
    # Parent guards clean separate child groups after even an abrupt controller exit.
    if owned and hubful and not adapter.wait(lambda: not adapter.port_open(job), seconds=5):
        raise TapError('Owned Hub did not release its port; component cleanup incomplete')
