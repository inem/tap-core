#!/usr/bin/env python3
"""Opt-in real system-proxy switch acceptance. Run in the owner's terminal.

Requires a temporarily stopped legacy proxy and sudo authorization. No keychain
changes. A failed cleanup retains the profile and prints its recovery command.
"""
import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import platform
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
from tap_core.runtime import MacOS, Profile
from tap_core.routing import SystemProxyRouting


def routing_state(state):
    # Disabled endpoint preferences may remain cached; they do not route traffic.
    return {name: {'bypass': row['bypass'], **{
        kind: value if value['enabled'] else {'enabled': False}
        for kind, value in row.items() if kind != 'bypass'}} for name, row in state.items()}


class Origin(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'{"fixture":"routing-switch"}'
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--allow-system-routing', action='store_true', required=True)
    args = parser.parse_args()
    adapter = MacOS()
    before = routing_state(adapter.network_state())
    if any(row[kind]['enabled'] for row in before.values() for kind in ('http', 'https')):
        raise RuntimeError('An existing system proxy is enabled. Stop the legacy TAP first; nothing changed.')
    subprocess.run(['/usr/bin/sudo', '-v'], check=True)
    directory = Path(tempfile.mkdtemp(prefix='tap-routing-live-', dir='/private/tmp'))
    code = directory / 'code'
    code.mkdir()
    shutil.copytree(SOURCE / 'tap_core', code / 'tap_core', ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copyfile(SOURCE / 'tap', code / 'tap')
    origin = ThreadingHTTPServer(('127.0.0.1', 0), Origin)
    threading.Thread(target=origin.serve_forever, daemon=True).start()
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    root = directory / 'profile'
    prefix = [sys.executable, str(code / 'tap'), '--profile', str(root)]
    report = {'source_commit': subprocess.check_output(['git', '-C', str(SOURCE), 'rev-parse', 'HEAD'], text=True).strip(),
              'code_sha256': {str(p.relative_to(code)): hashlib.sha256(p.read_bytes()).hexdigest()
                              for p in sorted(code.rglob('*')) if p.is_file()},
              'macOS': platform.mac_ver()[0], 'architecture': platform.machine(),
              'python': platform.python_version(), 'backend': 'mitmproxy 12.2.3',
              'scope': 'real system preference switch + explicit loopback HTTP probe; no CA/browser/managed-component acceptance',
              'checks': [], 'success': False, 'cleanup_verified': False}

    def cli(*args):
        result = subprocess.run(prefix + list(args), text=True, capture_output=True, timeout=60)
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip())
        return result.stdout

    def check(condition, name):
        if not condition:
            raise RuntimeError('Failed: ' + name)
        report['checks'].append(name)
        print('OK ' + name, flush=True)

    def state():
        return json.loads(cli('status'))

    def interrupted(*_):
        raise KeyboardInterrupt()

    signal.signal(signal.SIGTERM, interrupted)
    try:
        cli('install', '--backend', str(args.backend.resolve()), '--port', str(port),
            '--routing', 'explicit', '--probe-url', f'http://127.0.0.1:{origin.server_port}/fixture')
        cli('on')
        check(state()['routing'] == 'explicit', 'explicit started')
        cli('routing', 'set', 'system')
        p = Profile.load(root)
        check(SystemProxyRouting(p, adapter).verified(), 'running explicit to system applied to all services')
        snapshot = p.snapshot.read_bytes()
        cli('routing', 'set', 'system')
        check(p.snapshot.read_bytes() == snapshot, 'same-mode retains recovery snapshot')
        cli('routing', 'set', 'explicit')
        check(routing_state(adapter.network_state()) == before, 'running system to explicit restores routing and bypasses')
        check(state()['routing'] == 'explicit', 'profile returns to explicit')
        cli('off')
        cli('routing', 'set', 'system')
        p = Profile.load(root)
        check(not adapter.service_loaded(p) and routing_state(adapter.network_state()) == before,
              'stopped switch does not start or arm proxy')
        cli('on')
        check(SystemProxyRouting(p, adapter).verified(), 'on applies saved system mode')
        cli('off')
        check(routing_state(adapter.network_state()) == before and not adapter.service_loaded(p),
              'system off restores before stopping')
        cli('routing', 'set', 'explicit')
        check(Profile.load(root).routing == 'explicit', 'stopped system to explicit')
        report['success'] = True
    except BaseException as error:
        report['error'] = type(error).__name__ + ': ' + str(error)
    finally:
        # A second interrupt must not abort recovery. Never erase a profile whose
        # network restoration or owned job cleanup is uncertain.
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        try:
            if (root / 'profile.json').exists():
                cli('uninstall')
                p = Profile.load(root)
                check(not adapter.service_loaded(p) and not adapter.port_open(p), 'owned service removed')
            check(routing_state(adapter.network_state()) == before, 'final routing and bypasses restored')
            report['cleanup_verified'] = True
        except BaseException as error:
            report['cleanup_error'] = str(error)
            report['retained_profile'] = str(root)
            import shlex
            print('RECOVERY REQUIRED: ' + shlex.join(prefix + ['off']), file=sys.stderr)
        origin.shutdown()
        origin.server_close()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + '\n')
        print('Report: ' + str(args.output), flush=True)
        if report['cleanup_verified']:
            shutil.rmtree(directory)
    return 0 if report['success'] and report['cleanup_verified'] else 1


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
