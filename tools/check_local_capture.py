#!/usr/bin/env python3
"""Bounded macOS experiment, defaulting to an explicit-proxy control.

--mode local may install/launch the backend's shared Redirector and request OS
approval. It selects only this harness's own clients. No system proxy, CA trust,
launchd job or production TAP files are changed. Exit 2 means NOT a routing pass.
"""
import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import platform
import select
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'fixtures' / 'local-capture'
DIRECT = {'status': 200, 'body': 'tap-core-direct\n'}
INTERCEPTED = {'status': 200, 'body': 'tap-core-intercepted\n'}


def run(command):
    return subprocess.run(command, capture_output=True, text=True, timeout=15)


def extension_state():
    result = run(['/usr/bin/systemextensionsctl', 'list'])
    return {'inspection_exit': result.returncode,
            'mitmproxy_entries': [x.strip() for x in result.stdout.splitlines() if 'mitmproxy' in x.lower()],
            'inspection_failed': result.returncode != 0}


def proxy_snapshot():
    result = run(['/usr/sbin/scutil', '--proxy'])
    if result.returncode:
        raise RuntimeError('Cannot inspect effective proxy settings')
    return result.stdout


def stop(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    return process.returncode


class Client:
    def __init__(self):
        self.process = subprocess.Popen([sys.executable, '-B', '-u', str(FIXTURE / 'client.py')],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, text=True)

    def request(self, port, target):
        if self.process.poll() is not None:
            raise RuntimeError('Fixture client exited')
        self.process.stdin.write(json.dumps({'port': port, 'target': target}) + '\n')
        self.process.stdin.flush()
        if not select.select([self.process.stdout], [], [], 5)[0]:
            raise RuntimeError('Fixture client timed out')
        line = self.process.stdout.readline()
        if not line:
            raise RuntimeError('Fixture client closed its output')
        return json.loads(line)


def read_events(root):
    path = root / 'events.jsonl'
    if not path.exists():
        return []
    # Concurrent final append may be incomplete; it is not yet an event.
    return [json.loads(x) for x in path.read_text().splitlines(keepends=True) if x.endswith('\n')]


def check(args):
    if sys.platform != 'darwin':
        raise RuntimeError('This acceptance tool requires macOS')
    report = {'schema_version': 1, 'mode': args.mode, 'evidence': 'not_completed',
              'platform': {'macos': platform.mac_ver()[0], 'arch': platform.machine(),
                           'python': platform.python_version()},
              'source_sha256': {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                                for p in [Path(__file__).resolve(), *sorted(FIXTURE.glob('*.py'))]},
              'not_tested': ['HTTPS/CA/injection', 'UDP/IPv6', 'application bundle/helper identity',
                             'PID reuse', 'unknown audit token', 'existing-connection rule changes',
                             'simultaneous Local Capture profiles', 'clean-Mac install/update/remove']}
    version = run([args.backend, '--version'])
    if version.returncode:
        raise RuntimeError('Backend version check failed')
    report['backend_version'] = version.stdout.strip().splitlines()
    before = proxy_snapshot()
    report['extension_before'] = extension_state()
    report['cases'] = []
    with tempfile.TemporaryDirectory(prefix='tap-local-capture-') as directory:
        root = Path(directory)
        path = '/tap-core-routing-fixture-' + uuid.uuid4().hex

        class Origin(BaseHTTPRequestHandler):
            def do_GET(self):
                body = DIRECT['body'].encode() if self.path == path else b'fixture-only\n'
                self.send_response(200 if self.path == path else 404)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *unused):
                pass

        origin = ThreadingHTTPServer(('127.0.0.1', 0), Origin)
        origin.daemon_threads = True
        thread = threading.Thread(target=origin.serve_forever, daemon=True)
        thread.start()
        clients = []
        backend = None
        reservation = socket.socket()
        try:
            for unused in range(2):
                clients.append(Client())
            a, b = clients
            port = origin.server_port
            target = 'http://127.0.0.1:%d%s' % (port, path)
            if [client.request(port, path) for client in clients] != [DIRECT, DIRECT]:
                raise RuntimeError('Direct baseline failed')
            report['cases'].append({'case': 'direct_baseline', 'passed': True})
            config = {'mode': args.mode, 'root': str(root), 'origin_port': port,
                      'path': path, 'client_pids': [x.process.pid for x in clients]}
            (root / 'config.json').write_text(json.dumps(config))
            reservation.bind(('127.0.0.1', 0))
            proxy_port = reservation.getsockname()[1]
            mode = f'local:{a.process.pid}' if args.mode == 'local' else f'regular@127.0.0.1:{proxy_port}'
            command = [args.backend, '--mode', mode, '--set', 'confdir=' + str(root / 'certs'),
                       '--set', 'connection_strategy=lazy', '-q', '-s', str(FIXTURE / 'addon.py')]
            environment = dict(os.environ, TAP_LOCAL_FIXTURE_CONFIG=str(root / 'config.json'),
                               PYTHONDONTWRITEBYTECODE='1')
            with (root / 'backend.log').open('w+') as log:
                reservation.close()  # Backend does not accept an inherited listener.
                backend = subprocess.Popen(command, env=environment, stdout=log, stderr=log)
                deadline = time.monotonic() + args.startup_timeout
                while backend.poll() is None and time.monotonic() < deadline:
                    if any(x['event'] == 'running' for x in read_events(root)):
                        break
                    time.sleep(0.1)
                ready = backend.poll() is None and any(x['event'] == 'running' for x in read_events(root))
                if not ready:
                    stop(backend)
                    report['evidence'] = 'blocked_startup'
                    report['blocker'] = 'backend_start_failed_or_timed_out'
                else:
                    def pair(expected, route_port, route_target):
                        deadline = time.monotonic() + 5
                        attempts = 0
                        while True:
                            if backend.poll() is not None:
                                raise RuntimeError('Backend exited during routing check')
                            values = [a.request(route_port, route_target), b.request(port, path)]
                            attempts += 1
                            if values == expected or time.monotonic() >= deadline:
                                return {'passed': values == expected, 'observed': values,
                                        'attempts': attempts}
                            time.sleep(0.1)

                    if args.mode == 'explicit-control':
                        result = pair([INTERCEPTED, DIRECT], proxy_port, target)
                        report['cases'].append(dict(case='explicit_marker_control', **result))
                    else:
                        result = pair([INTERCEPTED, DIRECT], port, path)
                        report['cases'].append(dict(case='selected_A_unselected_B', **result))
                        if result['passed']:
                            temporary = root / 'control.tmp'
                            temporary.write_text(json.dumps({'pid': b.process.pid, 'generation': 1}))
                            temporary.replace(root / 'control.json')
                            swapped = pair([DIRECT, INTERCEPTED], port, path)
                            report['cases'].append(dict(case='swap_A_to_B_new_connections', **swapped))
                    report['evidence'] = 'passed' if all(x['passed'] for x in report['cases']) else 'failed_routing'
                report['addon_observations'] = read_events(root)
                stop(backend)
                log.flush()
                log.seek(0)
                detail = log.read()
                if report['evidence'] == 'blocked_startup' and any(
                        x in detail.lower() for x in ['redirector', 'system extension', 'network extension']):
                    report['blocker'] = 'redirector_setup_or_activation_required'
                if args.private_log:
                    args.private_log.write_text(detail)
                    args.private_log.chmod(0o600)
        except Exception as error:
            report['evidence'] = 'failed_harness'
            report['error'] = type(error).__name__ + ': ' + str(error)
        finally:
            if backend is not None:
                report['backend_exit'] = stop(backend)
            reservation.close()
            try:
                report['after_stop_direct'] = len(clients) == 2 and all(
                    client.request(origin.server_port, path) == DIRECT for client in clients)
            except Exception:
                report['after_stop_direct'] = False
            for client in clients:
                stop(client.process)
                client.process.stdin.close()
                client.process.stdout.close()
            report['own_processes_stopped'] = all(x.process.poll() is not None for x in clients)
            origin.shutdown()
            origin.server_close()
            thread.join(timeout=3)
        report['extension_after'] = extension_state()
        if report['evidence'] == 'blocked_startup' and any(
                'waiting for user' in x for x in report['extension_after']['mitmproxy_entries']):
            report['blocker'] = 'network_extension_awaiting_user_approval'
        report['effective_proxy_settings_unchanged'] = before == proxy_snapshot()
    report['temporary_profile_removed'] = not root.exists()
    if not all(report.get(k, False) for k in ['after_stop_direct', 'own_processes_stopped',
                                             'effective_proxy_settings_unchanged', 'temporary_profile_removed']):
        report['evidence'] = 'failed_cleanup'
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend', default='mitmdump')
    parser.add_argument('--mode', choices=['explicit-control', 'local'], default='explicit-control')
    parser.add_argument('--startup-timeout', type=float, default=15)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--private-log', type=Path)
    args = parser.parse_args()
    if not 1 <= args.startup_timeout <= 60:
        parser.error('startup timeout must be between 1 and 60 seconds')
    report = check(args)
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'mode': report['mode'], 'evidence': report['evidence'],
                      'blocker': report.get('blocker'), 'output': str(args.output)}))
    return 0 if report['evidence'] == 'passed' else 2


if __name__ == '__main__':
    sys.exit(main())
