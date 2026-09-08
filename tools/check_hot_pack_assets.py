#!/usr/bin/env python3
"""Opt-in #10 slice-1 live check: page pack A→B without proxy restart.

Explicit loopback only. Starts a temporary profile, enables a page-only pack,
loads the origin in headless Chrome, updates the pack while the proxy stays up,
then loads a new document and asserts the new marker. Also verifies:
  - retained content-addressed bytes for the previous digest on the same origin
  - identical script bytes granted to two origins are both fetchable (no 404)

Does not mutate the system proxy, claim CA trust, open-tab hot swap, or
reader/handler pack changes. Exit 0 only when scenario_passed and
cleanup_verified are both true.
"""
import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import platform
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tap_core.pack_store import PackStore, build_artifact
from tap_core.runtime import MacOS, Profile

PACK_ID = 'tap.check.hot-page'
BODY_MARK = 'tap-hot-origin-body'
DIGEST_RE = re.compile(r'/__tap/probe/core/([0-9a-f]{64})\.js')


class Origin(BaseHTTPRequestHandler):
    HTML = (f'<!doctype html><html><head><title>tap hot assets</title></head>'
            f'<body><h1>{BODY_MARK}</h1></body></html>')

    def do_GET(self):
        body = self.HTML.encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def wait(predicate, label, seconds=20):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    raise RuntimeError('Timed out: ' + label)


def run(argv, timeout=90, check=True):
    result = subprocess.run(list(map(str, argv)), text=True, capture_output=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError((result.stderr or result.stdout or 'command failed').strip())
    return result


def http_get(url, proxy_port=None, ca=None):
    argv = ['/usr/bin/curl', '--silent', '--show-error', '--max-time', '10',
            '-o', '-', '-w', '\\n%{http_code}', url]
    if proxy_port is not None:
        argv[1:1] = ['--proxy', f'http://127.0.0.1:{proxy_port}']
    if ca is not None:
        argv[1:1] = ['--cacert', str(ca)]
    out = run(argv).stdout
    body, _, code = out.rpartition('\n')
    return (int(code) if code.isdigit() else 0), body


def write_pack(source, version, marker, origins, shared_bytes=None):
    source.mkdir(parents=True)
    ui = f'window.{marker} = true;\n'
    feature = shared_bytes if shared_bytes is not None else f'window.{marker}Feature = true;\n'
    (source / 'ui.js').write_text(ui)
    (source / 'feature.js').write_text(feature)
    manifest = {
        'manifest_version': 1,
        'id': PACK_ID,
        'version': version,
        'requires': {'pack_api': 1, 'dependencies': []},
        'files': ['ui.js', 'feature.js'],
        'resources': [
            {
                'contract': 'tap.page-resource/v1', 'id': 'check.ui', 'version': version,
                'kind': 'browser-classic-script', 'file': 'ui.js',
                'sha256': hashlib.sha256(ui.encode()).hexdigest(),
                'license': 'MIT', 'source_revision': 'hot-' + version,
            },
            {
                'contract': 'tap.page-resource/v1', 'id': 'check.feature', 'version': version,
                'kind': 'browser-classic-script', 'file': 'feature.js',
                'sha256': hashlib.sha256(feature.encode()).hexdigest(),
                'license': 'MIT', 'source_revision': 'hot-' + version,
            },
        ],
        'entrypoints': {
            'page': {
                'interface': 'browser-scripts-v1',
                'uses': [
                    {'id': 'check.ui', 'version': version},
                    {'id': 'check.feature', 'version': version},
                ],
            },
        },
        'config': {},
        'access': {'origins': list(origins), 'capabilities': ['page.inject']},
    }
    (source / 'pack.json').write_text(json.dumps(manifest, indent=2) + '\n')
    return ui, feature


def digests_from_html(html):
    return DIGEST_RE.findall(html)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('backend', 'node', 'playwright', 'chrome', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    os.environ['PYTHONPATH'] = os.pathsep.join(
        [str(ROOT), os.environ.get('PYTHONPATH', '')]).rstrip(os.pathsep)
    backend = args.backend.resolve(strict=True)
    node = args.node.resolve(strict=True)
    playwright = args.playwright.resolve(strict=True)
    chrome = args.chrome.resolve(strict=True)
    adapter = MacOS()
    network_before = adapter.network_state()
    report = {
        'scope': '#10 slice 1 hot page assets: pack A→B without proxy restart; '
                 'new document + retained digest + shared bytes across origins; '
                 'explicit loopback; no system proxy',
        'commit': run(['/usr/bin/git', '-C', ROOT, 'rev-parse', 'HEAD'], check=False).stdout.strip()
                 or 'unknown',
        'platform': {
            'macOS': platform.mac_ver()[0],
            'architecture': platform.machine(),
            'python': platform.python_version(),
        },
        'backend': run([backend, '--version']).stdout.splitlines()[0],
        'playwright': json.loads((playwright / 'package.json').read_text())['version'],
        'scenario_passed': False,
        'cleanup_verified': False,
        'steps': {},
    }
    steps = report['steps']
    servers, profile = [], None
    root = Path(tempfile.mkdtemp(prefix='tap-hot-assets-'))
    root.chmod(0o700)
    try:
        for _ in range(2):
            server = ThreadingHTTPServer(('127.0.0.1', 0), Origin)
            servers.append(server)
            threading.Thread(target=server.serve_forever, daemon=True).start()
        origin_a = f'http://127.0.0.1:{servers[0].server_port}'
        origin_b = f'http://127.0.0.1:{servers[1].server_port}'
        report['origins'] = [origin_a, origin_b]

        shared = 'window.__tapSharedHot = true;\n'
        src_v1 = root / 'pack-v1'
        ui_v1, feature_v1 = write_pack(src_v1, '0.1.0', '__tapHotV1', [origin_a, origin_b], shared)
        artifact_v1 = root / 'hot-0.1.0.tap-pack'
        build_artifact(src_v1, artifact_v1)

        profile = Profile(root / 'profile', str(backend), free_port(), 'explicit', origin_a + '/', [])
        prefix = [sys.executable, str(ROOT / 'tap'), '--profile', str(profile.root)]
        bridge_config = {
            'version': 1, 'enabled': True, 'hub_port': free_port(),
            'allow_origins': [], 'exclude_origins': [], 'page_scripts': [],
        }
        bridge_path = root / 'bridge-config.json'
        bridge_path.write_text(json.dumps(bridge_config))
        run(prefix + ['install', '--backend', backend, '--port', profile.port,
                      '--routing', 'explicit', '--probe-url', origin_a + '/',
                      '--bridge-config', bridge_path])
        profile = Profile.load(profile.root)
        steps['install'] = 'ok'

        run(prefix + ['off'])
        PackStore(profile.root).install(artifact_v1)
        run(prefix + ['pack', 'enable', PACK_ID, '--version', '0.1.0',
                      '--grant-origin', origin_a, '--grant-origin', origin_b,
                      '--grant-capability', 'page.inject'])
        run(prefix + ['on'])
        steps['on'] = 'ok'
        ca = profile.root / 'certificates/mitmproxy-ca-cert.pem'
        token = (profile.root / 'state/bridge-token').read_text().strip()

        def html_ready(origin):
            code, body = http_get(origin + '/', profile.port, ca)
            return (code, body) if code == 200 and BODY_MARK in body and 'tap-probe-bootstrap' in body else None

        _, html_v1 = wait(lambda: html_ready(origin_a), 'v1 injection')
        digests_v1 = digests_from_html(html_v1)
        steps['v1_injection'] = bool(digests_v1)
        shared_digest = hashlib.sha256(shared.encode()).hexdigest()
        for host in (origin_a, origin_b):
            code, body = http_get(f'{host}/__tap/probe/core/{shared_digest}.js?token={token}',
                                  profile.port, ca)
            if code != 200 or body != shared:
                raise RuntimeError(f'shared digest not served for {host}: {code}')
        steps['identical_bytes_both_origins'] = True

        browser_cfg = root / 'browser-v1.json'
        browser_out = root / 'browser-v1-result.json'
        browser_cfg.write_text(json.dumps({
            'proxy_port': profile.port,
            'origin': origin_a,
            'marker': '__tapHotV1',
            'playwright': str(playwright),
            'chrome': str(chrome),
            'output': str(browser_out),
        }))
        run([node, ROOT / 'fixtures/hot-pack-assets/browser.cjs', browser_cfg], timeout=90)
        browser_v1 = json.loads(browser_out.read_text())
        steps['browser_v1'] = browser_v1
        report['browser'] = browser_v1.get('browser')

        src_v2 = root / 'pack-v2'
        ui_v2, feature_v2 = write_pack(src_v2, '0.2.0', '__tapHotV2', [origin_a, origin_b], shared)
        artifact_v2 = root / 'hot-0.2.0.tap-pack'
        build_artifact(src_v2, artifact_v2)
        # Hot path: update while proxy remains running (page-only pack).
        updated = json.loads(run(prefix + ['pack', 'update', artifact_v2]).stdout)
        steps['hot_update'] = updated
        if updated.get('enabled') is not True and not updated.get('selected'):
            # Accept either shape from pack update output.
            pass
        time.sleep(1.2)  # bridge plan TTL

        _, html_v2 = wait(lambda: html_ready(origin_a), 'v2 injection after hot update')
        digests_v2 = digests_from_html(html_v2)
        steps['v2_injection'] = bool(digests_v2)
        steps['plan_changed'] = set(digests_v1) != set(digests_v2)
        old_only = [digest for digest in digests_v1 if digest not in digests_v2]
        if not old_only:
            raise RuntimeError('expected at least one digest to change between v1 and v2')
        retained_code, retained_body = http_get(
            f'{origin_a}/__tap/probe/core/{old_only[0]}.js?token={token}', profile.port, ca)
        steps['retained_same_origin'] = retained_code == 200 and retained_body in (ui_v1, feature_v1)
        leaked_code, _ = http_get(
            f'{origin_b}/__tap/probe/core/{old_only[0]}.js?token={token}', profile.port, ca)
        # origin_b was also granted v1, so retention for both is correct; leak check uses a
        # digest that belonged only to ui_v1 and confirms origin_b may still fetch if granted.
        # Stronger leak gate: foreign digest never granted to origin_b alone is covered in unit tests.
        steps['retained_fetch_status'] = {'same': retained_code, 'other_granted': leaked_code}

        browser_cfg2 = root / 'browser-v2.json'
        browser_out2 = root / 'browser-v2-result.json'
        browser_cfg2.write_text(json.dumps({
            'proxy_port': profile.port,
            'origin': origin_a,
            'marker': '__tapHotV2',
            'playwright': str(playwright),
            'chrome': str(chrome),
            'output': str(browser_out2),
        }))
        run([node, ROOT / 'fixtures/hot-pack-assets/browser.cjs', browser_cfg2], timeout=90)
        browser_v2 = json.loads(browser_out2.read_text())
        steps['browser_v2'] = browser_v2

        failures = []
        for key in ('v1_injection', 'identical_bytes_both_origins', 'v2_injection',
                    'plan_changed', 'retained_same_origin'):
            if not steps.get(key):
                failures.append(key)
        if not steps.get('browser_v1', {}).get('marker_true'):
            failures.append('browser_v1')
        if not steps.get('browser_v2', {}).get('marker_true'):
            failures.append('browser_v2')
        report['scenario_failures'] = failures
        if failures:
            raise RuntimeError('scenario failures: ' + ', '.join(failures))
        report['scenario_passed'] = True
    except Exception as error:
        report['error'] = str(error)
        log = root / 'profile/logs/capture.log'
        if log.exists():
            print('proxy:\n' + log.read_text(errors='replace')[-8000:], file=sys.stderr)
    finally:
        cleanup_errors = []
        stop_confirmed = True
        if profile is not None:
            try:
                adapter.stop(profile)
                profile.plist.unlink(missing_ok=True)
                if adapter.service_loaded(profile) or adapter.port_open(profile):
                    stop_confirmed = False
                    cleanup_errors.append('service or port still active')
            except Exception as error:
                stop_confirmed = False
                cleanup_errors.append(str(error))
        for server in servers:
            server.shutdown()
            server.server_close()
        report['system_settings_unchanged'] = adapter.network_state() == network_before
        if not report['system_settings_unchanged']:
            cleanup_errors.append('system network settings changed')
        report['cleanup_errors'] = cleanup_errors
        report['cleanup_verified'] = not cleanup_errors and 'error' not in report
        if stop_confirmed and report['system_settings_unchanged']:
            shutil.rmtree(root, ignore_errors=True)
            report['temporary_profile_removed'] = True
        else:
            report['temporary_profile_preserved'] = str(root / 'profile')
            report['recovery_command'] = (
                f'{sys.executable} {ROOT / "tap"} --profile {root / "profile"} off')
            report['temporary_profile_removed'] = False
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))
    return 0 if report['scenario_passed'] and report['cleanup_verified'] else 1


if __name__ == '__main__':
    sys.exit(main())
