#!/usr/bin/env python3
"""Live-check the installed YouTube page pack in a fresh isolated profile.

This reaches the public YouTube site with a fresh headless browser. It bypasses
certificate errors inside that test browser, so it is live installed-artifact
evidence but not the #38 CA-trust acceptance result.
"""

import argparse
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tap_core.pack_store import PackStore, build_artifact


class Probe(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'probe'
        self.send_response(200)
        self.send_header('content-type', 'text/plain')
        self.send_header('content-length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


@contextmanager
def reserve_port():
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    try:
        yield listener
    finally:
        listener.close()


def run(args, timeout=90):
    result = subprocess.run(list(map(str, args)), text=True, capture_output=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result.stdout


def records(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend', type=Path, required=True)
    parser.add_argument('--node', type=Path, required=True)
    parser.add_argument('--playwright', type=Path, required=True)
    parser.add_argument('--chrome', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--url', default='https://www.youtube.com/watch?v=dQw4w9WgXcQ')
    args = parser.parse_args()
    for name in ('backend', 'node', 'chrome'):
        path = getattr(args, name).resolve()
        if not path.is_file():
            parser.error(f'--{name} must be an existing file')
        setattr(args, name, path)
    args.playwright = args.playwright.resolve()
    if not (args.playwright / 'package.json').is_file():
        parser.error('--playwright must be a package directory')

    with tempfile.TemporaryDirectory(prefix='tap-installed-youtube-') as directory, \
            reserve_port() as proxy, reserve_port() as hub:
        root = Path(directory)
        profile = root / 'profile'
        artifact = root / 'example.youtube-copy-links-0.1.0.tap-pack'
        bridge = root / 'bridge.json'
        browser_config = root / 'browser.json'
        browser_output = root / 'browser-result.json'
        bridge.write_text(json.dumps({'version': 1, 'enabled': True,
                                      'hub_port': hub.getsockname()[1],
                                      'allow_origins': [], 'exclude_origins': [],
                                      'page_scripts': []}))
        build_artifact(ROOT / 'examples/youtube-copy-links', artifact)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Probe)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        prefix = [sys.executable, '-B', ROOT / 'tap', '--profile', profile]
        proxy_port = proxy.getsockname()[1]
        proxy.close()
        hub.close()
        started = False
        try:
            run(prefix + ['install', '--backend', args.backend, '--port', proxy_port,
                          '--routing', 'explicit', '--probe-url',
                          f'http://127.0.0.1:{server.server_port}/', '--bridge-config', bridge])
            started = True
            run(prefix + ['off'])
            run(prefix + ['pack', 'install', artifact])
            run(prefix + ['pack', 'enable', 'example.youtube-copy-links', '--version', '0.1.0',
                          '--grant-origin', 'https://www.youtube.com',
                          '--grant-origin', 'https://youtube.com',
                          '--grant-capability', 'page.inject'])
            run(prefix + ['on'])
            explanation = json.loads(run(prefix + ['bridge', 'explain', '--origin',
                                                    'https://www.youtube.com']))
            browser_config.write_text(json.dumps({'playwright': str(args.playwright),
                                                   'chrome': str(args.chrome),
                                                   'proxy_port': proxy_port, 'url': args.url,
                                                   'output': str(browser_output)}))
            try:
                run([args.node, ROOT / 'fixtures/youtube-installed/browser.cjs', browser_config],
                    timeout=120)
            except RuntimeError as error:
                failure = json.loads(browser_output.read_text()) if browser_output.exists() else {}
                failure.update({'evidence': 'live installed artifact failed before UI acceptance',
                                'runner_error': str(error)})
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps(failure, indent=2) + '\n')
                print(json.dumps(failure, indent=2))
                raise
            result = json.loads(browser_output.read_text())
            installed = json.loads(run(prefix + ['pack', 'list']))
            version = installed['packs']['example.youtube-copy-links']['selected']
            code = profile / 'packs/example.youtube-copy-links/versions' / version
            saved_profile = json.loads((profile / 'profile.json').read_text())
            effective = PackStore(profile).effective_bridge(saved_profile['bridge'])
            deadline = time.monotonic() + 5
            captured = []
            while time.monotonic() < deadline:
                captured = [item for item in records(profile / 'data/stream.jsonl')
                            if item.get('url', '').startswith('https://www.youtube.com/')]
                if captured:
                    break
                time.sleep(0.1)
            relative_scripts = [str(Path(path).resolve().relative_to(profile.resolve()))
                                for path in effective['page_scripts']]
            result.update({'artifact_sha256': hashlib.sha256(artifact.read_bytes()).hexdigest(),
                           'installed_version': version,
                           'installed_code': str(code.relative_to(profile)),
                           'installed_page_scripts': relative_scripts,
                           'source_checkout_not_bound': all(str(ROOT) not in path
                                                            for path in effective['page_scripts']),
                           'origin_grant_enforced': explanation['allowed'],
                           'youtube_records_observed': len(captured),
                           'evidence': 'live installed artifact; CA trust bypassed in fresh browser'})
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(result, indent=2) + '\n')
            print(json.dumps(result, indent=2))
        finally:
            if started:
                subprocess.run(list(map(str, prefix + ['off'])), capture_output=True, text=True, timeout=30)
            server.shutdown()


if __name__ == '__main__':
    main()
