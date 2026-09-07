"""Profile-scoped bootstrap and route for the existing page/Hub wire protocol.

Trusted classic page scripts; no installed pack host, TLS bypass or app routing.
This file also loads directly as a mitmproxy addon, so imports are stdlib only.
"""
import hashlib
import hmac
import html
import json
import os
from pathlib import Path
import re
import stat
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

PREFIX = '/__tap/probe/'
MARKER = 'tap-probe-bootstrap'
SCRIPT_LIMIT = 256 * 1024


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate bridge configuration key')
        result[key] = value
    return result


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'), object_pairs_hook=unique_object)


def exact_origin(value):
    if not isinstance(value, str):
        raise ValueError('Bridge origin must be a canonical HTTP(S) origin')
    url = urlsplit(value)
    host, port = url.hostname, url.port
    if (url.scheme not in ('http', 'https') or not host
            or not re.fullmatch(r'[a-z0-9]+(?:[.-][a-z0-9]+)*', host)
            or url.username is not None or url.password is not None
            or port == {'http': 80, 'https': 443}.get(url.scheme)
            or port == 0 or value != url.scheme + '://' + host + (f':{port}' if port else '')):
        raise ValueError('Bridge origin must be exact canonical HTTP(S), without path or wildcard')
    return value


def configuration(value):
    if (not isinstance(value, dict)
            or set(value) != {'version', 'enabled', 'hub_port', 'allow_origins', 'exclude_origins', 'page_scripts'}
            or type(value['version']) is not int or value['version'] != 1
            or type(value['enabled']) is not bool
            or type(value['hub_port']) is not int or not 1024 <= value['hub_port'] <= 65535):
        raise ValueError('Invalid bridge configuration version/fields/enabled/hub_port')
    for name in ('allow_origins', 'exclude_origins', 'page_scripts'):
        items = value[name]
        if (not isinstance(items, list) or len(items) > 64
                or not all(isinstance(item, str) for item in items) or len(set(items)) != len(items)):
            raise ValueError('Bridge lists must contain at most 64 unique strings')
    for origin in value['allow_origins'] + value['exclude_origins']:
        exact_origin(origin)
    for script in value['page_scripts']:
        if not Path(script).is_absolute() or '\0' in script:
            raise ValueError('Bridge page scripts require explicit absolute paths')
    return value


def read_scripts(config):
    scripts = []
    for name in config['page_scripts']:
        if not Path(name).is_file():
            raise ValueError('Bridge page script must be a regular file')
        with Path(name).open('rb') as handle:
            body = handle.read(SCRIPT_LIMIT + 1)
        if len(body) > SCRIPT_LIMIT:
            raise ValueError('Bridge page script exceeds 256 KiB')
        body.decode('utf-8')
        scripts.append(body)
    return scripts


def fingerprint(config):
    return hashlib.sha256(json.dumps(config, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def decision(config, origin):
    exact_origin(origin)
    reason = ('disabled' if not config['enabled'] else 'user_exclusion' if origin in config['exclude_origins']
              else 'explicit_allow' if origin in config['allow_origins'] else 'not_allowed')
    return {'origin': origin, 'allowed': reason == 'explicit_allow', 'reason': reason,
            'scope': 'page injection and reserved local route only',
            'capture_policy': 'unchanged', 'tls_policy': 'unchanged', 'app_scope': 'unsupported'}


def token_file(root):
    return Path(root) / 'state/bridge-token'


def read_token(root):
    path = token_file(root)
    if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode) or path.stat().st_mode & 0o777 != 0o600:
        raise ValueError('Bridge token must be a private regular file (0600)')
    token = path.read_text().strip()
    if not re.fullmatch('[0-9a-f]{48}', token):
        raise ValueError('Invalid bridge token')
    return token


class Bridge:
    def __init__(self, config=None, token=None, scripts=None):
        self.config, self.token, self.scripts = config, token, scripts

    def load(self, loader):
        root = Path(os.environ['TAP_CORE_PROFILE'])
        self.config = configuration(read_json(root / 'profile.json')['bridge'])
        self.token = read_token(root)
        self.scripts = read_scripts(self.config) if self.config['enabled'] else []
        state = {'pid': os.getpid(), 'configuration': fingerprint(self.config), 'enabled': self.config['enabled']}
        path = root / 'state/bridge.json'
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(state))
        temporary.chmod(0o600)
        temporary.replace(path)

    def origin(self, request):
        # Destination authority, not a supplied Host/forwarded-origin header.
        port = '' if request.port == {'http': 80, 'https': 443}.get(request.scheme) else f':{request.port}'
        return request.scheme + '://' + request.host.lower() + port

    def allowed(self, origin):
        try:
            return decision(self.config, origin)['allowed']
        except ValueError:
            # Unsupported authorities (e.g. IPv6 in this exact-origin slice)
            # never expand the allowlist or break ordinary capture traffic.
            return False

    def reply(self, flow, status, body=b'', ctype='text/plain'):
        from mitmproxy import http
        flow.response = http.Response.make(status, body, {'content-type': ctype, 'cache-control': 'no-store'})

    def requestheaders(self, flow):
        if flow.metadata.get('tap_core_bridge_handled'):
            return
        request = flow.request
        # Never accept caller-supplied routing authority for a local Hub.
        for name in ('x-tap-probe-token', 'x-tap-probe-origin'):
            request.headers.pop(name, None)
        origin = self.origin(request)
        allowed = self.allowed(origin)
        path = urlsplit(request.path)
        if not path.path.startswith(PREFIX):
            if allowed and request.headers.get('sec-fetch-dest', '') in ('', 'document'):
                request.headers.pop('if-none-match', None)
                request.headers.pop('if-modified-since', None)
            return
        flow.metadata['tap_core_bridge_handled'] = True
        pairs = parse_qsl(path.query, keep_blank_values=True)
        supplied = [value for key, value in pairs if key == 'token']
        # Strip credentials and query token even on rejection, before capture.
        request.path = urlunsplit(('', '', path.path, urlencode([(k, v) for k, v in pairs if k != 'token']), ''))
        for name in ('cookie', 'authorization', 'proxy-authorization'):
            request.headers.pop(name, None)
        if not allowed or len(supplied) != 1 or not hmac.compare_digest(supplied[0].encode(), self.token.encode()):
            self.reply(flow, 403, b'Bridge access denied')
            return
        ws = request.headers.get('upgrade', '').lower() == 'websocket'
        observed_origin = request.headers.get('origin')
        if (ws and observed_origin != origin) or (observed_origin and observed_origin != origin):
            self.reply(flow, 403, b'Bridge origin mismatch')
            return
        asset = re.fullmatch(re.escape(PREFIX) + r'core/([0-9]+)\.js', path.path)
        if asset:
            index = int(asset.group(1))
            if index >= len(self.scripts):
                self.reply(flow, 404)
            else:
                self.reply(flow, 200, self.scripts[index], 'application/javascript; charset=utf-8')
            return
        request.scheme, request.host, request.port = 'http', '127.0.0.1', self.config['hub_port']
        request.headers['host'] = f"127.0.0.1:{self.config['hub_port']}"
        request.headers['x-tap-probe-token'] = self.token
        request.headers['x-tap-probe-origin'] = origin

    request = requestheaders

    def response(self, flow):
        response = flow.response
        if response is None or response.stream or flow.metadata.get('tap_core_bridge_handled'):
            return
        if 'text/html' not in response.headers.get('content-type', '').lower():
            return
        if flow.request.headers.get('sec-fetch-dest', '') not in ('', 'document'):
            return
        if not self.allowed(self.origin(flow.request)):
            return
        body = response.get_text(strict=False)
        if body is None:
            print('[tap bridge] HTML unavailable; injection skipped', flush=True)
            return
        if re.search(r'\bid\s*=\s*[\"\']' + MARKER + r'[\"\']', body):
            return
        nonce = re.search(r'<script\b[^>]*\bnonce=[\"\']([A-Za-z0-9_+/-]{1,256}={0,2})[\"\']', body, re.I)
        nonce_attr = ' nonce="' + html.escape(nonce.group(1), quote=True) + '"' if nonce else ''
        scripts = [f'<script id="{MARKER}"{nonce_attr} data-tap-token="{self.token}" '
                   f'src="{PREFIX}runtime.js?token={self.token}"></script>']
        scripts += [f'<script{nonce_attr} src="{PREFIX}core/{index}.js?token={self.token}"></script>'
                    for index in range(len(self.scripts))]
        closing = re.search(r'</body\s*>', body, re.I)
        position = closing.start() if closing else len(body)
        response.set_text(body[:position] + ''.join(scripts) + body[position:])
        for name in ('etag', 'last-modified'):
            response.headers.pop(name, None)
        response.headers['cache-control'] = 'no-store'


addons = [Bridge()]
