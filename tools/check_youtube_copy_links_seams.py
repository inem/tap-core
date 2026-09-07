#!/usr/bin/env python3
"""Admit YouTube copy-links scripts into the profile bridge contract.

Fixture-only. Does not start a proxy, Hub, browser or system network change.
Live HTTPS/CSP/certificate evidence remains a separate check.
"""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tap_core.bridge import (  # noqa: E402
    MARKER,
    PREFIX,
    Bridge,
    DocumentScripts,
    configuration,
    decision,
    fingerprint,
    read_scripts,
)

EXAMPLE = ROOT / 'examples' / 'youtube-copy-links'
UI = EXAMPLE / 'youtube-ui.js'
BOOTSTRAP = EXAMPLE / 'copy-links.js'
ORIGINS = ['https://www.youtube.com', 'https://youtube.com']


def bridge_config(hub_port=19002, proxy_port=19001):
    if type(proxy_port) is not int or not 1024 <= proxy_port <= 65535:
        raise ValueError('Proxy port must be an integer in 1024..65535')
    if hub_port == proxy_port:
        raise ValueError('Bridge Hub and proxy must use different ports')
    return configuration({
        'version': 1,
        'enabled': True,
        'hub_port': hub_port,
        'allow_origins': list(ORIGINS),
        'exclude_origins': [],
        'page_scripts': [str(UI.resolve()), str(BOOTSTRAP.resolve())],
    })


def check_files():
    assert UI.is_file() and BOOTSTRAP.is_file(), 'example scripts missing'
    ui = UI.read_text(encoding='utf-8')
    boot = BOOTSTRAP.read_text(encoding='utf-8')
    assert 'window.YouTubeUI' in ui or 'YouTubeUI =' in ui, 'youtube-ui.js missing API'
    assert 'window.__tapYoutubeCopyLinks' in boot, 'bootstrap marker missing'
    assert 'if (!window.YouTubeUI) return;' in boot, 'bootstrap must require YouTubeUI'
    assert 'addVideoCardAction' in boot and 'addWatchVideoAction' in boot
    assert 'clipboard.writeText' in boot
    # Delivery changed: no custom mutator asset path and no CSP strip in this package.
    assert '/__tap/youtube-copy-links.js' not in boot
    assert 'content-security-policy' not in boot.lower()
    import hashlib
    assert hashlib.sha256(UI.read_bytes()).hexdigest() == (
        '77317dfe7a6708eb0d96ce465ce619aefb5a226b4c4ebaa8c16c78540cc467ed')
    assert hashlib.sha256(BOOTSTRAP.read_bytes()).hexdigest() == (
        '2b3ceae0470ba11023534f49333df29f2540b9319b14e047779c4167bc852daf')
    return ui, boot


def check_port_collision():
    try:
        bridge_config(19001, 19001)
    except ValueError as error:
        assert 'different ports' in str(error)
    else:
        raise AssertionError('hub_port equal to proxy_port must be rejected')
    try:
        bridge_config(19002, 80)
    except ValueError as error:
        assert '1024' in str(error)
    else:
        raise AssertionError('proxy_port below 1024 must be rejected')


def check_admit(config):
    scripts = read_scripts(config)
    assert len(scripts) == 2
    assert scripts[0].startswith(UI.read_bytes()[:64])
    assert b'__tapYoutubeCopyLinks' in scripts[1]
    for origin in ORIGINS:
        result = decision(config, origin)
        assert result['allowed'] is True and result['reason'] == 'explicit_allow'
        assert result['capture_policy'] == 'unchanged'
        assert result['tls_policy'] == 'unchanged'
        assert result['app_scope'] == 'unsupported'
    denied = decision(config, 'https://music.youtube.com')
    assert denied['allowed'] is False and denied['reason'] == 'not_allowed'
    return scripts


class _Response:
    def __init__(self, body, headers=None):
        self._body = body
        self.headers = {
            'content-type': 'text/html; charset=utf-8',
            'content-security-policy': "script-src 'nonce-abc123'",
            'etag': 'W/"x"',
            **(headers or {}),
        }
        self.stream = False

    def get_text(self, strict=False):
        return self._body

    def set_text(self, value):
        self._body = value


class _Request:
    def __init__(self, host='www.youtube.com', scheme='https', port=443, path='/watch?v=dQw4w9WgXcQ'):
        self.host, self.scheme, self.port, self.path = host, scheme, port, path
        self.headers = {'sec-fetch-dest': 'document'}


class _Flow:
    def __init__(self, body):
        self.request = _Request()
        self.response = _Response(body)
        self.metadata = {}


class FixtureBridge(Bridge):
    """Avoid importing mitmproxy in the development interpreter."""

    def reply(self, flow, status, body=b'', ctype='text/plain'):
        flow.response = type('R', (), {'status_code': status, 'content': body, 'ctype': ctype})()


def check_injection(config, scripts):
    token = 'ab' * 24
    bridge = FixtureBridge(config=config, token=token, scripts=scripts)
    source = (
        '<!doctype html><html><head>'
        '<script nonce="abc123">window.yt={}</script>'
        '</head><body><ytd-app></ytd-app></body></html>'
    )
    flow = _Flow(source)
    csp_header = flow.response.headers['content-security-policy']
    bridge.response(flow)
    body = flow.response.get_text()
    assert flow.response.headers['content-security-policy'] == csp_header, 'CSP must stay intact'
    assert MARKER in body
    assert f'nonce="abc123"' in body
    assert f'{PREFIX}runtime.js?token={token}' in body
    assert f'{PREFIX}core/0.js?token={token}' in body
    assert f'{PREFIX}core/1.js?token={token}' in body
    assert body.index('core/0.js') < body.index('core/1.js')
    assert body.index(MARKER) < body.index('</body>')
    # Absolute asset URLs from the intercepted origin, not a foreign base.
    assert 'https://www.youtube.com/__tap/probe/core/0.js' in body
    parsed = DocumentScripts(body)
    assert parsed.has_bootstrap is True
    # Second pass must not duplicate bootstrap.
    again = _Flow(body)
    bridge.response(again)
    assert again.response.get_text().count(MARKER) == 1
    assert again.response.get_text().count('core/0.js') == 1


def check_asset_routes(config, scripts):
    token = 'cd' * 24
    bridge = FixtureBridge(config=config, token=token, scripts=scripts)

    class Req:
        def __init__(self, path, host='www.youtube.com'):
            self.host, self.scheme, self.port, self.path = host, 'https', 443, path
            self.headers = {}

    class Flow:
        def __init__(self, path):
            self.request = Req(path)
            self.response = None
            self.metadata = {}

    ok = Flow(f'{PREFIX}core/0.js?token={token}')
    bridge.requestheaders(ok)
    assert ok.response.status_code == 200
    assert ok.response.content == scripts[0]
    boot = Flow(f'{PREFIX}core/1.js?token={token}')
    bridge.requestheaders(boot)
    assert boot.response.status_code == 200
    assert b'__tapYoutubeCopyLinks' in boot.response.content
    bad = Flow(f'{PREFIX}core/0.js?token={"00" * 24}')
    bridge.requestheaders(bad)
    assert bad.response.status_code == 403
    # runtime.js is not an in-memory asset: it is rewritten toward the Hub.
    runtime = Flow(f'{PREFIX}runtime.js?token={token}')
    bridge.requestheaders(runtime)
    assert runtime.response is None
    assert runtime.request.host == '127.0.0.1'
    assert runtime.request.port == config['hub_port']
    # music.youtube.com is outside allow_origins
    foreign = Flow(f'{PREFIX}core/0.js?token={token}')
    foreign.request.host = 'music.youtube.com'
    bridge.requestheaders(foreign)
    assert foreign.response.status_code == 403


def write_config(path, config):
    path = Path(path)
    path.write_text(json.dumps(config, indent=2) + '\n')
    path.chmod(0o600)
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--write-config', type=Path, help='Write admitted bridge JSON with absolute script paths')
    parser.add_argument('--hub-port', type=int, default=19002)
    parser.add_argument('--proxy-port', type=int, default=19001,
                        help='Profile proxy port; must differ from --hub-port (Profile rejects equality)')
    args = parser.parse_args()

    check_files()
    check_port_collision()
    config = bridge_config(args.hub_port, args.proxy_port)
    scripts = check_admit(config)
    check_injection(config, scripts)
    check_asset_routes(config, scripts)
    print(json.dumps({
        'ok': True,
        'example': str(EXAMPLE),
        'fingerprint': fingerprint(config),
        'proxy_port': args.proxy_port,
        'hub_port': config['hub_port'],
        'scripts': [
            {'path': config['page_scripts'][0], 'bytes': len(scripts[0])},
            {'path': config['page_scripts'][1], 'bytes': len(scripts[1])},
        ],
        'origins_allowed': ORIGINS,
        'csp': 'preserved_in_fixture_injection',
        'runtime_js': 'forwarded_to_hub_port',
        'core_assets': 'served_from_bridge_memory',
        'evidence': 'fixture',
    }, indent=2))
    if args.write_config:
        written = write_config(args.write_config, config)
        print(f'wrote {written}', file=sys.stderr)


if __name__ == '__main__':
    main()
