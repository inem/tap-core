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


class HTMLScanError(ValueError):
    """Markup cannot be safely scanned for this limited injection operation."""


class DocumentScripts:
    """Scan complete HTML for nonce/marker attributes and a body insertion point.

    This forward-only tokenizer skips comments and text-only elements, retains
    only two attributes per tag, and caps their encoded values at 4096 characters.
    It does not build an HTML5 tree. Ambiguous/truncated markup skips injection.
    """
    SPACE = re.compile(r'[ \t\n\f\r]*')
    COMMENT_END = re.compile(r'--!?>')
    DOCTYPE = re.compile(r'<!doctype(?=[ \t\n\f\r>])', re.I | re.ASCII)
    TAG = re.compile(r'<(/?)([A-Za-z][^ \t\n\f\r/>]*)')
    ATTRIBUTE = re.compile(r'''([^ \t\n\f\r"'<>/=]+)(?:[ \t\n\f\r]*=[ \t\n\f\r]*(?:"([^"]*)"|'([^']*)'|([^ \t\n\f\r>"'<]+)))?''')
    ENTITY = re.compile(r'&#(?:[xX][0-9a-fA-F]+|[0-9]+);?|&[A-Za-z][A-Za-z0-9]+;')
    RAW = {'script', 'style', 'textarea', 'title', 'xmp', 'iframe', 'noembed', 'noframes', 'noscript'}

    @classmethod
    def attribute_value(cls, raw):
        if raw is None:
            return None
        if len(raw) > 4096:
            raise HTMLScanError('Attribute exceeds scanner limit')
        def decode(match):
            value = html.unescape(match.group())
            # Preserve invalid/control references instead of silently dropping
            # them and accidentally making an invalid nonce or marker valid.
            return value if re.fullmatch(r'[A-Za-z0-9_+/=-]+', value) else match.group()
        return cls.ENTITY.sub(decode, raw)

    @classmethod
    def tag_end(cls, body, position):
        values = {}
        while position < len(body):
            position = cls.SPACE.match(body, position).end()
            if body.startswith('>', position):
                return position + 1, values
            if body.startswith('/>', position):
                return position + 2, values
            attribute = cls.ATTRIBUTE.match(body, position)
            if attribute is None:
                raise HTMLScanError('Incomplete or unsupported tag')
            name = attribute[1].lower()
            if name in ('nonce', 'id') and name not in values:
                raw = next((value for value in attribute.groups()[1:] if value is not None), None)
                values[name] = cls.attribute_value(raw)
            position = attribute.end()
        raise HTMLScanError('Unclosed tag')

    def __init__(self, body):
        self.nonce, self.has_bootstrap, self.body_end = '', False, None
        position = 0
        while position < len(body):
            start = body.find('<', position)
            if start < 0:
                break
            if body.startswith('<!--', start):
                if body.startswith(('<!-->', '<!--->'), start):
                    raise HTMLScanError('Ambiguous comment opener')
                end = self.COMMENT_END.search(body, start + 4)
                if end is None:
                    raise HTMLScanError('Unclosed comment')
                position = end.end()
                continue
            if self.DOCTYPE.match(body, start):
                # Quoted identifiers may contain >; other declarations are
                # outside this scanner's scope and leave the response intact.
                position = start + 9
                while position < len(body) and body[position] != '>':
                    if body[position] in ('"', "'"):
                        end = body.find(body[position], position + 1)
                        if end < 0:
                            raise HTMLScanError('Unclosed doctype identifier')
                        position = end + 1
                    elif body[position] in '<[':
                        raise HTMLScanError('Unsupported declaration')
                    else:
                        position += 1
                if position == len(body):
                    raise HTMLScanError('Unclosed doctype')
                position += 1
                continue
            if body.startswith(('<!', '<?'), start):
                raise HTMLScanError('Unsupported declaration')
            tag = self.TAG.match(body, start)
            if tag is None:
                position = start + 1
                continue
            closing, name = tag[1], tag[2].lower()
            position, values = self.tag_end(body, tag.end())
            if closing:
                if name == 'body' and self.body_end is None:
                    self.body_end = start
                continue
            if values.get('id') == MARKER:
                self.has_bootstrap = True
            nonce = values.get('nonce')
            if name == 'script' and not self.nonce and isinstance(nonce, str) and re.fullmatch(r'[A-Za-z0-9_+/-]{1,256}={0,2}', nonce):
                self.nonce = nonce
            if name == 'plaintext':
                raise HTMLScanError('Plaintext has no script insertion point')
            if name in self.RAW:
                end = re.compile(r'</' + name + r'(?=[ \t\n\f\r/>])', re.I | re.ASCII).search(body, position)
                if end is None:
                    raise HTMLScanError('Unclosed text-only element')
                if name == 'script' and body.find('<!--', position, end.start()) >= 0:
                    # Legacy escaped/double-escaped script states need HTML5
                    # parsing; do not mistake their apparent end tags for markup.
                    raise HTMLScanError('Unsupported script comment state')
                position, _ = self.tag_end(body, end.end())


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
        origin = self.origin(request)
        allowed = self.allowed(origin)
        path = urlsplit(request.path)
        if not path.path.startswith(PREFIX):
            if allowed and request.headers.get('sec-fetch-dest', '') in ('', 'document'):
                request.headers.pop('if-none-match', None)
                request.headers.pop('if-modified-since', None)
            return
        # Only the reserved route interprets these as local Hub authority.
        # Ordinary site traffic retains its own headers unchanged.
        for name in ('x-tap-probe-token', 'x-tap-probe-origin'):
            request.headers.pop(name, None)
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
        try:
            parsed = DocumentScripts(body)
        except HTMLScanError:
            print('[tap bridge] HTML parsing failed; injection skipped', flush=True)
            return
        if parsed.has_bootstrap:
            return
        nonce_attr = ' nonce="' + html.escape(parsed.nonce, quote=True) + '"' if parsed.nonce else ''
        # Document <base> must never redirect token-bearing bootstrap/assets.
        asset_root = html.escape(self.origin(flow.request) + PREFIX, quote=True)
        scripts = [f'<script id="{MARKER}"{nonce_attr} data-tap-token="{self.token}" '
                   f'src="{asset_root}runtime.js?token={self.token}"></script>']
        scripts += [f'<script{nonce_attr} src="{asset_root}core/{index}.js?token={self.token}"></script>'
                    for index in range(len(self.scripts))]
        position = parsed.body_end if parsed.body_end is not None else len(body)
        response.set_text(body[:position] + ''.join(scripts) + body[position:])
        for name in ('etag', 'last-modified'):
            response.headers.pop(name, None)
        response.headers['cache-control'] = 'no-store'


addons = [Bridge()]
