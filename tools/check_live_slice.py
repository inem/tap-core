#!/usr/bin/env python3
"""Opt-in #29 live slice against explicitly supplied trusted legacy source.

Temporary launchd profile, foreground Hub, separate headless Chrome, loopback
HTTP/WS. No system proxy mutation or CA trust. No installed-pack claim.
"""
import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import platform
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from urllib.request import ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tap_core.runtime import MacOS, Profile
from tap_core.records import decode_record

FIXTURES = ROOT / 'fixtures/live-slice'
SOURCE_FILES = ('probe/hub.js', 'probe/runtime.js', 'probe/adapter-runtime.js')


class Origin(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/record':
            body, ctype = json.dumps({'value': self.server.fixture_value}).encode(), 'application/json'
        elif self.path == '/':
            body = ('<!doctype html><html><head><title>TAP live slice</title>'
                    '<base href="' + self.server.foreign_base + '/assets/">'
                    '<script ' + self.server.nonce_attribute + '>window.fixtureCSP=true;</script>'
                    '</head><body><h1>TAP live slice</h1><button id="load">Read captured result</button>'
                    '<pre id="result">waiting</pre><span id="confirmed">pending</span>'
                    '</body></html>').encode()
            ctype = 'text/html'
        else:
            body, ctype = b'not found', 'text/plain'
        self.send_response(200 if self.path in ('/', '/record') else 404)
        self.send_header('Content-Type', ctype)
        if self.path == '/':
            self.send_header('Content-Security-Policy', "script-src 'nonce-dGFwLWZpeHR1cmU'")
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
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
        time.sleep(0.05)
    raise RuntimeError('Timed out: ' + label)


def run(argv, timeout=45):
    result = subprocess.run(list(map(str, argv)), text=True, capture_output=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(result.stderr or result.stdout or 'command failed')
    return result.stdout


def read_lines(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_bytes().split(b'\n')[:-1]]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('source', 'backend', 'bun', 'node', 'playwright', 'chrome'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    for name in ('source', 'backend', 'bun', 'node', 'playwright', 'chrome'):
        setattr(args, name, getattr(args, name).resolve(strict=True))
    hashes = {name: hashlib.sha256((args.source / name).read_bytes()).hexdigest() for name in SOURCE_FILES}
    adapter = MacOS()
    network_before = adapter.network_state()
    origins, children, handles = [], [], []
    profile = None
    report = {'scope': 'live loopback HTTP + WS in isolated launchd profile and headless Chrome; synthetic data',
              'source_sha256': hashes, 'platform': {'macOS': platform.mac_ver()[0], 'architecture': platform.machine(),
                                                   'python': platform.python_version()},
              'backend': run([args.backend, '--version']).splitlines()[0],
              'bun': run([args.bun, '--version']).strip(),
              'playwright': json.loads((args.playwright / 'package.json').read_text())['version']}
    with tempfile.TemporaryDirectory(prefix='tap-live-slice-') as directory:
        root = Path(directory)
        root.chmod(0o700)
        def spawn(argv, label):
            handle = (root / (label + '.log')).open('wb')
            handles.append(handle)
            child = subprocess.Popen(list(map(str, argv)), stdout=handle, stderr=handle, start_new_session=True)
            children.append(child)
            return child
        try:
            nonce = secrets.token_hex(12)
            for _ in range(3):
                server = ThreadingHTTPServer(('127.0.0.1', 0), Origin)
                server.fixture_value = nonce
                origins.append(server)
                threading.Thread(target=server.serve_forever, daemon=True).start()
            origin, second_origin, denied = [f'http://127.0.0.1:{server.server_port}' for server in origins]
            for index, server in enumerate(origins):
                server.foreign_base = denied
                server.nonce_attribute = 'nonce = "dGFwLWZpeHR1cmU"' if index == 0 else 'nonce=dGFwLWZpeHR1cmU'
            for name in ('empty-adapters', 'empty-flows', 'probe'):
                (root / name).mkdir(mode=0o700)
            config = {'root': str(root), 'source': str(args.source), 'hub_port': free_port(),
                      'proxy_port': free_port(), 'token': secrets.token_hex(24), 'origin': origin,
                      'denied_origin': denied, 'second_origin': second_origin, 'playwright': str(args.playwright), 'chrome': str(args.chrome)}
            config_path = root / 'fixture.json'
            config_path.write_text(json.dumps(config))
            config_path.chmod(0o600)
            bridge_config = {'version': 1, 'enabled': True, 'hub_port': config['hub_port'],
                             'allow_origins': [origin, second_origin, denied],
                             'exclude_origins': [denied], 'page_scripts': [str(FIXTURES / 'page.js')]}
            bridge_path = root / 'bridge-config.json'
            bridge_path.write_text(json.dumps(bridge_config))
            opener = build_opener(ProxyHandler({}))
            def control(path, payload=None):
                req = Request(f"http://127.0.0.1:{config['hub_port']}" + path,
                              data=json.dumps(payload).encode() if payload is not None else None,
                              headers={'Authorization': 'Bearer ' + config['token'], 'Content-Type': 'application/json'})
                with opener.open(req, timeout=15) as response:
                    return json.load(response)
            def hub_ready():
                if hub.poll() is not None:
                    raise RuntimeError('Hub failed: ' + (root / 'hub.log').read_text())
                try:
                    return control('/v1/pages')['ok']
                except OSError:
                    return False
            profile = Profile(root / 'profile', str(args.backend), config['proxy_port'], 'explicit', origin + '/record', [])
            prefix = [sys.executable, ROOT / 'tap', '--profile', profile.root]
            run(prefix + ['install', '--backend', args.backend, '--port', profile.port, '--routing', 'explicit',
                          '--probe-url', profile.probe_url, '--bridge-config', bridge_path])
            profile = Profile.load(profile.root)
            config['token'] = (profile.root / 'state/bridge-token').read_text().strip()
            config_path.write_text(json.dumps(config))
            hub = spawn([args.bun, FIXTURES / 'hub.mjs', config_path], 'hub')
            wait(hub_ready, 'Hub startup')
            assert read_lines(root / 'bus.jsonl') == []
            report['hub_started_empty'] = True
            run(prefix + ['on'])
            assert json.loads(run(prefix + ['doctor']))['healthy']
            # Distinguish the browser's request from install/on/doctor probes.
            origins[0].fixture_value = secrets.token_hex(12)
            expected = origins[0].fixture_value
            browser = spawn([args.node, FIXTURES / 'browser.cjs', config_path], 'browser')
            def requested():
                if browser.poll() is not None:
                    raise RuntimeError('Browser stopped before request: ' + (root / 'browser.log').read_text())
                return next((event for event in read_lines(root / 'bus.jsonl')
                             if event.get('payload', {}).get('name') == 'fixture.output.requested'), None)
            event = wait(requested, 'page request over WS', seconds=35)
            stream = profile.root / 'data/stream.jsonl'
            def captured():
                for record in read_lines(stream):
                    if record.get('url') == origin + '/record' and record.get('body') == json.dumps({'value': expected}):
                        return decode_record(json.dumps(record), allow_legacy=False)
            captured_record = wait(captured, 'capture writer flush')
            spec_path = root / 'reader.json'
            spec_path.write_text(json.dumps({'version': 1, 'revision': 'live-slice-1',
                                 'command': [sys.executable, str(FIXTURES / 'reader.py')],
                                 'config': {'url': origin + '/record'}}))
            progress = json.loads(run(prefix + ['reader', 'run', 'projection', '--definition', spec_path]))
            projection = json.loads((profile.root / 'data/readers/projection/result.json').read_text())
            assert projection == {'record_id': captured_record['record_id'], 'value': expected}
            page_id = event['pageId']
            result = control(f'/v1/pages/{page_id}/commands', {'name': 'fixture.render', 'args': projection})
            assert result['payload']['ok'] and result['payload']['value'] == dict(projection, rendered=True), result
            # Confirmation lets the browser exit after the controller observed the
            # Result from the page, without racing socket teardown against its ack.
            control(f'/v1/pages/{page_id}/commands', {'name': 'fixture.confirm', 'args': {}})
            browser.wait(timeout=20)
            if browser.returncode:
                raise RuntimeError('Browser failed: ' + (root / 'browser.log').read_text())
            browser_result = json.loads((root / 'browser-result.json').read_text())
            assert browser_result['displayed'] == projection
            assert {'Hello', 'Event', 'Result'} <= set(browser_result['frames']['sent'])
            assert {'Welcome', 'Command'} <= set(browser_result['frames']['received'])
            assert browser_result['other_tab_unchanged'] and browser_result['denied_origin_unchanged']
            # A configured user exclusion overrides an allow entry on real pages.
            explanation = json.loads(run(prefix + ['bridge', 'explain', '--origin', denied]))
            assert explanation['reason'] == 'user_exclusion' and not explanation['allowed']
            bridge_config['enabled'] = False
            bridge_path.write_text(json.dumps(bridge_config))
            changing = subprocess.run(list(map(str, prefix + ['bridge', 'configure', '--config', bridge_path])),
                                      text=True, capture_output=True, timeout=15)
            assert changing.returncode == 1 and 'Stop this profile' in changing.stderr
            run(prefix + ['off'])
            run(prefix + ['bridge', 'configure', '--config', bridge_path])
            run(prefix + ['on'])
            config['disabled'] = True
            config_path.write_text(json.dumps(config))
            disabled_browser = spawn([args.node, FIXTURES / 'browser.cjs', config_path], 'disabled-browser')
            disabled_browser.wait(timeout=30)
            if disabled_browser.returncode:
                raise RuntimeError('Disabled browser failed: ' + (root / 'disabled-browser.log').read_text())
            assert json.loads((root / 'disabled-result.json').read_text())['not_injected']
            report['profile_bridge'] = {'two_allowed_origins': True, 'user_exclusion_overrides_allow': True,
                                        'live_reconfigure_rejected': True, 'off_configure_on_disables': True,
                                        'legacy_injector_or_generated_addon_needed': False,
                                        'spaced_and_unquoted_nonce_with_csp': True,
                                        'foreign_base_asset_origin_preserved': True,
                                        'foreign_origin_token_requests': browser_result['foreign_origin_token_requests']}
            assert hashes == {name: hashlib.sha256((args.source / name).read_bytes()).hexdigest() for name in SOURCE_FILES}
            report.update({'capture_reader_projection_page_match': True, 'controller_received_page_result': True,
                           'reader_records_processed': progress['completed_this_run'],
                           'browser': browser_result['browser'], 'websocket_frame_kinds': browser_result['frames'],
                           'other_tab_unchanged': True, 'denied_origin_not_injected': True,
                           'legacy_sources_unchanged': True,
                           'transport': {'http_body': 'verified_live', 'own_page_hub_ws': 'verified_live',
                                         'https_ca_trust': 'not_tested', 'sse': 'not_tested',
                                         'third_party_ws_capture': 'not_tested', 'chatgpt_conduit': 'not_tested'}})
        finally:
            cleanup_errors = []
            for child in reversed(children):
                if child.poll() is None:
                    try:
                        os.killpg(child.pid, signal.SIGTERM)
                        child.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(child.pid, signal.SIGKILL)
                        child.wait(timeout=5)
                    except Exception as error:
                        cleanup_errors.append(str(error))
            if profile is not None:
                try:
                    adapter.stop(profile)
                    profile.plist.unlink(missing_ok=True)
                    assert not adapter.service_loaded(profile) and not adapter.port_open(profile)
                except Exception as error:
                    cleanup_errors.append(str(error))
            for server in origins:
                server.shutdown()
                server.server_close()
            for handle in handles:
                handle.close()
            if cleanup_errors:
                raise RuntimeError('Cleanup failed: ' + '; '.join(cleanup_errors))
        report['system_settings_unchanged'] = adapter.network_state() == network_before
        assert report['system_settings_unchanged']
    report['temporary_profile_and_jobs_removed'] = True
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
