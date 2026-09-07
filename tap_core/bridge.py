"""Profile-scoped bootstrap and route for the existing page/Hub wire protocol.

Trusted classic page scripts and installed page-pack snapshots; no TLS bypass or
app routing. This file also loads directly as a mitmproxy addon, so imports are
stdlib only.
"""
import hashlib
import hmac
import html
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

PREFIX = '/__tap/probe/'
MARKER = 'tap-probe-bootstrap'
SCRIPT_LIMIT = 256 * 1024
RESOURCE_ID = re.compile(r'[a-z][a-z0-9]*(?:[.-][a-z0-9]+)*\Z')
RESOURCE_VERSION = re.compile(r'(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z')
RESOURCE_DIGEST = re.compile(r'[0-9a-f]{64}\Z')
RESOURCE_CONTRACT = 'tap.page-resource/v1'


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


def configuration(value, script_origins=None):
    if (not isinstance(value, dict)
            or set(value) != {'version', 'enabled', 'hub_port', 'allow_origins', 'exclude_origins', 'page_scripts'}
            or type(value['version']) is not int or value['version'] != 1
            or type(value['enabled']) is not bool
            or type(value['hub_port']) is not int or not 1024 <= value['hub_port'] <= 65535):
        raise ValueError('Invalid bridge configuration version/fields/enabled/hub_port')
    for name in ('allow_origins', 'exclude_origins', 'page_scripts'):
        items = value[name]
        if (not isinstance(items, list) or len(items) > 64
                or not all(isinstance(item, str) for item in items)
                or (name != 'page_scripts' or script_origins is None)
                and len(set(items)) != len(items)):
            raise ValueError('Bridge lists must contain at most 64 unique strings')
    for origin in value['allow_origins'] + value['exclude_origins']:
        exact_origin(origin)
    for script in value['page_scripts']:
        if not Path(script).is_absolute() or '\0' in script:
            raise ValueError('Bridge page scripts require explicit absolute paths')
    if script_origins is not None:
        if (not isinstance(script_origins, list)
                or len(script_origins) != len(value['page_scripts'])):
            raise ValueError('Effective page script origins are malformed')
        seen = {}
        allowed = set(value['allow_origins'])
        for script, origins in zip(value['page_scripts'], script_origins):
            if (not isinstance(origins, list) or not origins or len(origins) > 64
                    or not all(isinstance(origin, str) for origin in origins)
                    or len(set(origins)) != len(origins) or not set(origins) <= allowed):
                raise ValueError('Effective page script origins are malformed')
            for origin in origins:
                exact_origin(origin)
            overlap = seen.setdefault(script, set()).intersection(origins)
            if overlap:
                raise ValueError(f'Page resource would be injected twice for {sorted(overlap)[0]}')
            seen[script].update(origins)
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


def order_page_resources(declarations, use_orders):
    """Return resource/origin entries preserving every pack's ordered uses.

    A single global topological order keeps one entry per resource when
    possible. If only disjoint origins disagree, emit origin-scoped entries;
    each document still receives a resource once. A cycle on one origin is an
    unsupported composition and must fail before activation.
    """
    priority = {resource_id: index for index, resource_id in enumerate(declarations)}

    def topological(nodes, edges):
        nodes = set(nodes)
        followers = {node: set() for node in nodes}
        incoming = {node: 0 for node in nodes}
        for before, after in edges:
            if before not in nodes or after not in nodes or after in followers[before]:
                continue
            followers[before].add(after)
            incoming[after] += 1
        key = lambda node: (priority[node], node)
        ready = sorted((node for node in nodes if incoming[node] == 0), key=key)
        ordered = []
        while ready:
            node = ready.pop(0)
            ordered.append(node)
            for after in sorted(followers[node], key=key):
                incoming[after] -= 1
                if incoming[after] == 0:
                    ready.append(after)
                    ready.sort(key=key)
        return ordered if len(ordered) == len(nodes) else None

    all_edges = set()
    for item in use_orders:
        all_edges.update(zip(item['resources'], item['resources'][1:]))
    ordered = topological(declarations, all_edges)
    if ordered is not None:
        return [(resource_id, sorted(declarations[resource_id]['origins']))
                for resource_id in ordered]

    origins = sorted({origin for item in use_orders for origin in item['origins']})
    grouped = {}
    for origin in origins:
        nodes = {resource_id for resource_id, declaration in declarations.items()
                 if origin in declaration['origins']}
        edges = set()
        for item in use_orders:
            if origin in item['origins']:
                edges.update(zip(item['resources'], item['resources'][1:]))
        sequence = topological(nodes, edges)
        if sequence is None:
            raise ValueError(f'Page resource order is cyclic for origin {origin}')
        grouped.setdefault(tuple(sequence), []).append(origin)
    result = []
    for sequence, scoped_origins in sorted(grouped.items(), key=lambda item: item[1][0]):
        result.extend((resource_id, scoped_origins) for resource_id in sequence)
    return result


def effective_configuration(root, base):
    """Resolve installed page packs without importing the checkout as a package.

    mitmproxy loads this file as a standalone addon. Keep this path stdlib-only
    and re-check the installed snapshot before any page code is read.
    """
    root = Path(root).resolve()
    try:
        registry = read_json(root / 'state/pack-registry.json')
    except FileNotFoundError:
        return base
    if (type(registry) is not dict or set(registry) != {'version', 'packs'}
            or registry['version'] != 1 or type(registry['packs']) is not dict):
        raise ValueError('Pack registry is malformed or incompatible')
    result = json.loads(json.dumps(base))
    base_origins = list(result['allow_origins'])
    script_origins = [list(base_origins) for _ in result['page_scripts']]
    declarations = {}
    use_orders = []
    any_enabled = False
    for pack_id in sorted(registry['packs']):
        record = registry['packs'][pack_id]
        if type(record) is not dict or type(record.get('enabled')) is not bool:
            raise ValueError(f'Pack registry entry is malformed: {pack_id}')
        if not record['enabled']:
            continue
        any_enabled = True
        version = record.get('selected')
        versions = record.get('versions')
        if type(version) is not str or type(versions) is not dict:
            raise ValueError(f'Enabled pack version is malformed: {pack_id}')
        metadata = versions.get(version)
        grants = record.get('grants')
        if type(metadata) is not dict or type(metadata.get('hashes')) is not dict or type(grants) is not dict:
            raise ValueError(f'Enabled pack registry entry is malformed: {pack_id}')
        code = root / 'packs' / pack_id / 'versions' / str(version)
        if code.is_symlink() or not code.is_dir() or code.resolve() != code:
            raise ValueError(f'Enabled pack code is missing or unsafe: {pack_id}@{version}')
        manifest = read_json(code / 'pack.json')
        if (type(manifest) is not dict or manifest.get('id') != pack_id or manifest.get('version') != version
                or type(manifest.get('files')) is not list or len(manifest['files']) > 256
                or not all(type(name) is str for name in manifest['files'])
                or len(manifest['files']) != len(set(manifest['files']))
                or type(manifest.get('entrypoints')) is not dict):
            raise ValueError(f'Enabled pack manifest is incompatible: {pack_id}@{version}')
        roles = set(manifest['entrypoints'])
        if not roles or not roles <= {'page', 'reader', 'handler'}:
            raise ValueError(f'Enabled pack has unsupported host roles: {pack_id}@{version}')
        access = manifest.get('access')
        if (type(access) is not dict or set(access) != {'origins', 'capabilities'}
                or type(access['origins']) is not list or type(access['capabilities']) is not list
                or not all(type(value) is str for value in access['origins'] + access['capabilities'])
                or not access['origins'] or len(access['origins']) != len(set(access['origins']))
                or len(access['capabilities']) != len(set(access['capabilities']))):
            raise ValueError(f'Enabled pack access is malformed: {pack_id}@{version}')
        requested_origins = access['origins']
        requested_capabilities = access['capabilities']
        if (not set(requested_origins) <= set(grants.get('origins', []))
                or not set(requested_capabilities) <= set(grants.get('capabilities', []))):
            raise ValueError(f'Enabled pack access is not granted: {pack_id}@{version}')
        dependencies = manifest.get('requires', {}).get('dependencies')
        inventory = grants.get('dependencies')
        if type(dependencies) is not list or type(inventory) is not dict:
            raise ValueError(f'Enabled pack dependencies are malformed: {pack_id}@{version}')
        for dependency in dependencies:
            if (type(dependency) is not dict or inventory.get(dependency.get('id')) != dependency.get('version')):
                raise ValueError(f'Enabled pack dependency is unavailable: {pack_id}@{version}')
        expected = {'pack.json', *manifest['files']}
        for name in expected:
            path = PurePosixPath(name)
            if (not name or path.is_absolute() or '..' in path.parts
                    or str(path) != name or '\\' in name or '\x00' in name):
                raise ValueError(f'Enabled pack contains an unsafe path: {pack_id}@{version}')
        observed = set()
        for path in code.rglob('*'):
            if path.is_symlink():
                raise ValueError(f'Enabled pack contains a symlink: {pack_id}@{version}')
            if path.is_file():
                observed.add(path.relative_to(code).as_posix())
        if observed != expected or set(metadata['hashes']) != expected:
            raise ValueError(f'Enabled pack file set changed: {pack_id}@{version}')
        for name in sorted(expected):
            path = code / name
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if metadata['hashes'].get(name) != digest:
                raise ValueError(f'Enabled pack integrity check failed: {pack_id}@{version}')
        if roles & {'page', 'handler'}:
            for origin in requested_origins:
                exact_origin(origin)
                if origin not in result['allow_origins']:
                    result['allow_origins'].append(origin)
        if 'page' not in roles:
            continue
        page = manifest['entrypoints']['page']
        resources = manifest.get('resources')
        if (type(page) is not dict or set(page) != {'interface', 'uses'}
                or page.get('interface') != 'browser-scripts-v1'
                or type(page.get('uses')) is not list or not page['uses']
                or type(resources) is not list or not resources
                or 'page.inject' not in requested_capabilities):
            raise ValueError(f'Enabled pack has no installed page binding: {pack_id}@{version}')
        resource_index = {}
        for resource in resources:
            if (type(resource) is not dict
                    or set(resource) != {'contract', 'id', 'version', 'kind', 'file', 'sha256',
                                         'license', 'source_revision'}
                    or resource.get('contract') != RESOURCE_CONTRACT
                    or type(resource.get('id')) is not str
                    or not RESOURCE_ID.fullmatch(resource['id'])
                    or type(resource.get('version')) is not str
                    or not RESOURCE_VERSION.fullmatch(resource['version'])
                    or resource.get('kind') != 'browser-classic-script'
                    or type(resource.get('file')) is not str
                    or type(resource.get('sha256')) is not str
                    or not RESOURCE_DIGEST.fullmatch(resource['sha256'])
                    or not all(type(resource.get(name)) is str and resource[name].strip()
                               for name in ('license', 'source_revision'))
                    or (resource['id'], resource['version']) in resource_index):
                raise ValueError(f'Enabled pack page providers are malformed: {pack_id}@{version}')
            resource_index[(resource['id'], resource['version'])] = resource
        use_ids = set()
        for use in page['uses']:
            if (type(use) is not dict or set(use) != {'id', 'version'}
                    or type(use['id']) is not str or not RESOURCE_ID.fullmatch(use['id'])
                    or use['id'] in use_ids or type(use['version']) is not str
                    or not RESOURCE_VERSION.fullmatch(use['version'])
                    or (use['id'], use['version']) not in resource_index):
                raise ValueError(f'Enabled pack page declarations are malformed: {pack_id}@{version}')
            use_ids.add(use['id'])
        for resource in resources:
            if (resource['file'] not in expected
                    or metadata['hashes'].get(resource['file']) != resource['sha256']):
                raise ValueError(f'Enabled pack page script is undeclared: {pack_id}@{version}')
        use_orders.append({'pack': pack_id, 'origins': tuple(requested_origins),
                           'resources': tuple(use['id'] for use in page['uses'])})
        for use in page['uses']:
            resource = resource_index[(use['id'], use['version'])]
            shared = (root / 'resources' / 'page' / resource['id'] / resource['version']
                      / (resource['sha256'] + '.js'))
            if (shared.is_symlink() or not shared.is_file() or shared.resolve() != shared
                    or hashlib.sha256(shared.read_bytes()).hexdigest() != resource['sha256']):
                raise ValueError(f"Shared page resource is missing or changed: "
                                 f"{resource['id']}@{resource['version']}")
            resource_id = use['id']
            declaration = {'version': use['version'],
                           'digest': resource['sha256'],
                           'path': str(shared),
                           'origins': set(requested_origins),
                           'pack': pack_id}
            current = declarations.get(resource_id)
            if current is None:
                declarations[resource_id] = declaration
                continue
            if current['version'] != declaration['version']:
                raise ValueError(f'Page resource {resource_id} requests conflicting versions: '
                                 f"{current['version']} and {declaration['version']}")
            if current['digest'] != declaration['digest']:
                raise ValueError(f"Page resource {resource_id}@{use['version']} has conflicting "
                                 f"content in {current['pack']} and {pack_id}")
            current['origins'].update(requested_origins)
    if not any_enabled:
        return base
    if declarations:
        for resource_id, origins in order_page_resources(declarations, use_orders):
            declaration = declarations[resource_id]
            result['page_scripts'].append(declaration['path'])
            script_origins.append(origins)
    configuration(result, script_origins)
    result['page_script_origins'] = script_origins
    return result


def decision(config, origin):
    exact_origin(origin)
    reason = ('disabled' if not config['enabled'] else 'user_exclusion' if origin in config['exclude_origins']
              else 'explicit_allow' if origin in config['allow_origins'] else 'not_allowed')
    return {'origin': origin, 'allowed': reason == 'explicit_allow', 'reason': reason,
            'scope': 'page injection and reserved local route only',
            'capture_policy': 'unchanged', 'tls_policy': 'unchanged', 'app_scope': 'unsupported'}


def token_file(root):
    return Path(root) / 'state/bridge-token'


def read_token(root, name='bridge-token'):
    path = Path(root) / 'state' / name
    try:
        mode = path.lstat().st_mode  # Inspect once, without following symlinks.
        if not stat.S_ISREG(mode) or mode & 0o777 != 0o600:
            raise ValueError(f'{name} must be a private regular file (0600): {path}')
        token = path.read_text(encoding='ascii').strip()
    except FileNotFoundError as error:
        raise ValueError(f'Missing {name} file: {path}') from error
    except OSError as error:
        raise ValueError(f'Cannot read {name} file: {path}: {error.strerror}') from error
    except UnicodeError as error:
        raise ValueError(f'Invalid {name}: expected ASCII hexadecimal text') from error
    if not re.fullmatch('[0-9a-f]{48}', token):
        raise ValueError(f'Invalid {name}: expected 48 lowercase hexadecimal characters')
    return token


class Bridge:
    def __init__(self, config=None, token=None, scripts=None):
        self.config, self.token, self.scripts = config, token, scripts
        self.script_origins = config.get('page_script_origins') if config else None
        self.component_token = None

    def load(self, loader):
        root = Path(os.environ['TAP_CORE_PROFILE'])
        profile = read_json(root / 'profile.json')
        self.config = effective_configuration(root, configuration(profile['bridge']))
        if profile.get('components') is not None:
            self.component_token = read_token(root, 'component-token')
        self.token = read_token(root)
        self.scripts = read_scripts(self.config) if self.config['enabled'] else []
        self.script_origins = self.config.get('page_script_origins')
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
        for name in ('x-tap-probe-token', 'x-tap-probe-origin', 'x-tap-component-token'):
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
            if (index >= len(self.scripts)
                    or (self.script_origins is not None
                        and origin not in self.script_origins[index])):
                self.reply(flow, 404)
            else:
                self.reply(flow, 200, self.scripts[index], 'application/javascript; charset=utf-8')
            return
        request.scheme, request.host, request.port = 'http', '127.0.0.1', self.config['hub_port']
        request.headers['host'] = f"127.0.0.1:{self.config['hub_port']}"
        request.headers['x-tap-probe-token'] = self.token
        request.headers['x-tap-probe-origin'] = origin
        if self.component_token:
            request.headers['x-tap-component-token'] = self.component_token

    request = requestheaders

    def response(self, flow):
        response = flow.response
        if response is None or response.stream or flow.metadata.get('tap_core_bridge_handled'):
            return
        if 'text/html' not in response.headers.get('content-type', '').lower():
            return
        if flow.request.headers.get('sec-fetch-dest', '') not in ('', 'document'):
            return
        origin = self.origin(flow.request)
        if not self.allowed(origin):
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
        indexes = [index for index in range(len(self.scripts))
                   if self.script_origins is None or origin in self.script_origins[index]]
        scripts += [f'<script{nonce_attr} src="{asset_root}core/{index}.js?token={self.token}"></script>'
                    for index in indexes]
        position = parsed.body_end if parsed.body_end is not None else len(body)
        response.set_text(body[:position] + ''.join(scripts) + body[position:])
        for name in ('etag', 'last-modified'):
            response.headers.pop(name, None)
        response.headers['cache-control'] = 'no-store'


addons = [Bridge()]
