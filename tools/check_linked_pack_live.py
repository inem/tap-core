#!/usr/bin/env python3
"""Opt-in #14 live browser check for the published linked-http pack.

Captures a real HTTP /record response through the TAP proxy, lets the installed
reader project it, clicks Load in headless Chrome, and asserts #result shows the
saved projection value (not the raw fetch body alone).

Explicit routing only. Does not mutate the system proxy, claim CA trust, SSE,
third-party WS, ChatGPT/conduit, or clean-Mac acceptance. Exit 0 only when
scenario_passed and cleanup_verified are both true.
"""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import platform
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tap_core.pack_store import PackStore
from tap_core.runtime import Lifecycle, MacOS, Profile
from tap_core.components import Job

PACK_ID = 'example.linked-http'
ORIGIN = 'http://127.0.0.1:18998'
ORIGIN_PORT = 18998
RELEASE = ('https://github.com/inem/tap-pack-linked-http/releases/download/'
           'v0.1.0/example.linked-http-0.1.0.tap-pack')
ORIGIN_HTML = ROOT / 'fixtures/linked-http/origin.html'


class LinkedOrigin(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith('/record'):
            body = json.dumps({'value': self.server.fixture_value}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('X-Fixture-Value', self.server.fixture_value)
            self.end_headers()
            self.wfile.write(body)
            return
        html = ORIGIN_HTML.read_bytes()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(html)))
        self.end_headers()
        self.wfile.write(html)

    def log_message(self, format, *args):
        pass


def run(argv, timeout=90, check=True):
    result = subprocess.run(list(map(str, argv)), text=True, capture_output=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError((result.stderr or result.stdout or 'command failed').strip())
    return result


def wait(predicate, label, seconds=30):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    raise RuntimeError('Timed out: ' + label)


def reserve_port():
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    return sock


def stream_records(path):
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text().splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('backend', 'bun', 'node', 'playwright', 'chrome', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--pack', type=Path)
    args = parser.parse_args()
    for name in ('backend', 'bun', 'node', 'playwright', 'chrome'):
        setattr(args, name, getattr(args, name).resolve(strict=True))
    if run([args.bun, '--version']).stdout.strip() != '1.3.11':
        raise SystemExit('This check requires Bun 1.3.11')

    adapter = MacOS()
    before = adapter.network_state()
    report = {
        'scope': 'installed linked-http pack live browser; explicit loopback; not clean-Mac/CA/SSE/third-party WS',
        'core_commit': run(['git', '-C', ROOT, 'rev-parse', 'HEAD']).stdout.strip(),
        'python': platform.python_version(),
        'macOS': platform.mac_ver()[0],
        'bun': run([args.bun, '--version']).stdout.strip(),
        'backend': run([args.backend, '--version']).stdout.splitlines()[0],
        'pack_id': PACK_ID,
        'origin': ORIGIN,
        'scenario_passed': False,
        'cleanup_verified': False,
        'transport': {
            'controlled_http_body': 'live_proxied_capture',
            'own_page_hub_ws': 'browser_TapBridge_request',
            'sse_body': 'not_claimed',
            'third_party_ws': 'not_claimed',
        },
    }

    # Fail early if the pack's fixed origin port is occupied.
    probe = socket.socket()
    try:
        probe.bind(('127.0.0.1', ORIGIN_PORT))
    except OSError as error:
        raise SystemExit('Port 18998 is required by example.linked-http and is busy: ' + str(error)) from error
    finally:
        probe.close()

    with tempfile.TemporaryDirectory(prefix='tap-linked-live-') as directory:
        root = Path(directory)
        profile_root = root / 'profile'
        profile = None
        server = None
        try:
            artifact = args.pack.resolve(strict=True) if args.pack else root / 'pack.tap-pack'
            if not args.pack:
                import urllib.request
                urllib.request.urlretrieve(RELEASE, artifact)
                report['artifact_source'] = 'release'
            else:
                report['artifact_source'] = 'pack'
            report['artifact_sha256'] = __import__('hashlib').sha256(artifact.read_bytes()).hexdigest()

            server = ThreadingHTTPServer(('127.0.0.1', ORIGIN_PORT), LinkedOrigin)
            server.fixture_value = 'seed'
            threading.Thread(target=server.serve_forever, daemon=True).start()

            with reserve_port() as proxy, reserve_port() as hub:
                bridge = dict(version=1, enabled=True, hub_port=hub.getsockname()[1],
                              allow_origins=[], exclude_origins=[], page_scripts=[])
                components = dict(version=1, python=sys.executable, bun=str(args.bun),
                                  readers={}, handlers={})
                profile = Profile(profile_root, str(args.backend), proxy.getsockname()[1], 'explicit',
                                  ORIGIN + '/record', [], bridge=bridge, components=components)
                (root / 'bridge.json').write_text(json.dumps(bridge))
                (root / 'components.json').write_text(json.dumps(components))
                proxy_port = profile.port
                hub_port = bridge['hub_port']
                proxy.close()
                hub.close()

            prefix = [sys.executable, str(ROOT / 'tap'), '--profile', str(profile_root)]
            run(prefix + ['install', '--backend', args.backend, '--port', proxy_port,
                          '--routing', 'explicit', '--probe-url', ORIGIN + '/record',
                          '--bridge-config', root / 'bridge.json',
                          '--components-config', root / 'components.json'])
            # Pack enable applies on next on; stop any auto-start from install first.
            Lifecycle(Profile.load(profile_root), adapter).off()
            store = PackStore(profile_root)
            store.install(artifact)
            store.enable(PACK_ID, '0.1.0', origins=[ORIGIN],
                         capabilities=['page.inject', 'capture.read', 'bridge.handle'])
            run(prefix + ['on'])
            doctor = json.loads(run(prefix + ['doctor']).stdout)
            assert doctor['healthy'], doctor
            report['install_enable_on'] = True

            def browser_round():
                value = secrets.token_hex(8)
                server.fixture_value = value
                out = root / ('browser-' + value + '.json')
                config = {
                    'proxy_port': proxy_port,
                    'origin': ORIGIN,
                    'expected_value': value,
                    'playwright': str(args.playwright),
                    'chrome': str(args.chrome),
                    'output': str(out),
                }
                cfg = root / 'browser-config.json'
                cfg.write_text(json.dumps(config))
                run([args.node, ROOT / 'fixtures/linked-http/browser.cjs', cfg], timeout=90)
                result = json.loads(out.read_text())
                assert result['displayed']['value'] == value
                # Real capture of the proxied /record response must exist.
                def captured():
                    for row in stream_records(profile_root / 'data/stream.jsonl'):
                        if row.get('url') == ORIGIN + '/record' and row.get('status') == 200:
                            try:
                                if json.loads(row.get('body') or '')['value'] == value:
                                    return row
                            except Exception:
                                pass
                    return None
                record = wait(captured, 'proxied HTTP capture for /record')
                assert record['record_id'] == result['displayed']['record_id']
                projection = json.loads(
                    (profile_root / 'data/readers' / PACK_ID / 'result.json').read_text())
                assert projection['value'] == value == result['displayed']['value']
                return {'displayed': result['displayed'], 'capture_record_id': record['record_id'],
                        'browser': result['browser']}

            report['browser'] = browser_round()
            checkpoint = profile_root / 'state/readers' / PACK_ID / 'checkpoint.json'
            progress = json.loads(checkpoint.read_text())['processed']
            run(prefix + ['off'])
            run(prefix + ['on'])
            assert json.loads(checkpoint.read_text())['processed'] >= progress
            report['restart_resume'] = True
            report['browser_after_restart'] = browser_round()
            report['scenario_passed'] = True
        except Exception as error:
            if profile is not None:
                for path in (profile_root / 'logs/capture.log', profile_root / 'logs/components.log'):
                    if path.is_file():
                        print(str(path) + ':\n' + path.read_text(errors='replace')[-8000:],
                              file=sys.stderr)
            raise RuntimeError(str(error)) from None
        finally:
            try:
                if profile is not None:
                    Lifecycle(profile, adapter).off()
                    assert not adapter.service_loaded(profile)
                    assert not adapter.service_loaded(Job(profile))
            finally:
                if server is not None:
                    server.shutdown()
                    server.server_close()
            report['cleanup_verified'] = adapter.network_state() == before
            if report['cleanup_verified']:
                report['system_settings_unchanged'] = True

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))
    if not (report['scenario_passed'] and report['cleanup_verified']):
        raise SystemExit(1)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(type(error).__name__ + ': ' + str(error), file=sys.stderr)
        raise SystemExit(1)
