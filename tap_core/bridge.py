"""Profile-scoped bootstrap and route for the existing page/Hub wire protocol.

Trusted classic page scripts and installed page-pack snapshots; no TLS bypass or
app routing. This file also loads directly as a mitmproxy addon, so imports are
stdlib only.

Page assets are content-addressed (`core/<sha256>.js`). The applied pack plan is
re-read from the profile on a short TTL without restarting the proxy; a failed
refresh keeps the previous plan. Open pages reconcile an origin-scoped plan over
the proxy; classic scripts use a page reload as their explicit lifecycle.
"""
import hashlib
import hmac
import html
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

PREFIX = '/__tap/probe/'
MARKER = 'tap-probe-bootstrap'
SCRIPT_LIMIT = 256 * 1024
PLAN_TTL_SECONDS = 1.0
RESOURCE_ID = re.compile(r'[a-z][a-z0-9]*(?:[.-][a-z0-9]+)*\Z')
RESOURCE_VERSION = re.compile(r'(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z')
RESOURCE_DIGEST = re.compile(r'[0-9a-f]{64}\Z')
RESOURCE_CONTRACT = 'tap.page-resource/v1'
ASSET_PATH = re.compile(re.escape(PREFIX) + r'core/([0-9a-f]{64})\.js\Z')
PLAN_PATH = PREFIX + 'plan.json'
RUNTIME_PATH = PREFIX + 'runtime.js'
PLAN_VERSION = 'tap.page-plan/v1'
ALL_ORIGINS = '*'


def development_configuration(root):
    """Read the private local-authoring overlay; absence means installed mode."""
    try:
        value = read_json(Path(root) / 'state/development.json')
    except FileNotFoundError:
        return {'version': 1, 'origins': [], 'tools': []}
    if (type(value) is not dict or set(value) != {'version', 'origins', 'tools'}
            or value['version'] != 1
            or type(value['origins']) is not list or len(value['origins']) > 64
            or type(value['tools']) is not list or len(value['tools']) > 16
            or len(value['origins']) != len(set(value['origins']))
            or len(value['tools']) != len(set(value['tools']))
            or not all(type(origin) is str for origin in value['origins'])
            or not all(type(tool) is str and RESOURCE_ID.fullmatch(tool) for tool in value['tools'])):
        raise ValueError('Development configuration is malformed')
    for origin in value['origins']:
        exact_origin(origin)
    return value


def valid_feature_folder(value):
    if type(value) is not str or not 1 <= len(value) <= 240:
        return False
    path = PurePosixPath(value)
    return (not path.is_absolute() and '..' not in path.parts and str(path) == value
            and '\\' not in value and '\x00' not in value
            and bool(path.parts) and path.parts[0] == 'data')


class HTMLScanError(ValueError):
    """Markup cannot be safely scanned for this limited injection operation."""


def authorize_script_policy(value, nonce):
    """Extend script elements only; keep other directives and each policy."""
    policies = []
    for policy in value.split(','):
        parts = policy.split(';')
        directives = {}
        for index, part in enumerate(parts):
            words = part.split()
            if words: directives.setdefault(words[0].lower(), (index, words[1:]))
        source = next((directives[k] for k in ('script-src-elem', 'script-src', 'default-src') if k in directives), None)
        if source is None:
            policies.append(policy); continue
        # Adding a nonce disables an otherwise active unsafe-inline source.
        # Preserve that policy rather than break the site's existing inline code.
        if "'unsafe-inline'" in source[1] and not any(w.startswith(("'nonce-", "'sha256-", "'sha384-", "'sha512-")) for w in source[1]):
            policies.append(policy); continue
        words = [word for word in source[1] if word.lower() != "'none'"]
        permission = "'nonce-" + nonce + "'"
        if permission not in words: words.append(permission)
        directive = 'script-src-elem ' + ' '.join(words)
        if 'script-src-elem' in directives: parts[directives['script-src-elem'][0]] = directive
        else: parts.append(directive)
        policies.append(';'.join(parts))
    return ','.join(policies)


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
    def tag_end(cls, body, position, meta=False):
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
            if (name in ('nonce', 'id') or meta and name in ('http-equiv', 'content')) and name not in values:
                raw = next((value for value in attribute.groups()[1:] if value is not None), None)
                values[name] = None if name == 'content' and raw is not None and len(raw) > 4096 else cls.attribute_value(raw)
            position = attribute.end()
        raise HTMLScanError('Unclosed tag')

    def __init__(self, body):
        self.nonce, self.has_bootstrap, self.body_end = '', False, None
        self.meta_policies = []
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
            position, values = self.tag_end(body, tag.end(), meta=name == 'meta')
            if closing:
                if name == 'body' and self.body_end is None:
                    self.body_end = start
                continue
            if values.get('id') == MARKER:
                self.has_bootstrap = True
            if name == 'meta' and (values.get('http-equiv') or '').lower() == 'content-security-policy' and not isinstance(values.get('content'), str):
                raise HTMLScanError('CSP meta content unavailable or oversized')
            if name == 'meta' and (values.get('http-equiv') or '').lower() == 'content-security-policy' and isinstance(values.get('content'), str):
                self.meta_policies.append((start, position, html.unescape(values['content'])))
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


def exact_or_all_origin(value):
    if value == ALL_ORIGINS:
        return value
    return exact_origin(value)


def origin_allowed(origin, origins):
    return ALL_ORIGINS in origins or origin in origins


def origin_subset(requested, allowed):
    return ALL_ORIGINS in allowed or set(requested) <= set(allowed)


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
    for origin in value['allow_origins']:
        exact_or_all_origin(origin)
    for origin in value['exclude_origins']:
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
                    or len(set(origins)) != len(origins) or not origin_subset(origins, allowed)):
                raise ValueError('Effective page script origins are malformed')
            for origin in origins:
                exact_or_all_origin(origin)
            overlap = seen.setdefault(script, set()).intersection(origins)
            wildcard_overlap = (ALL_ORIGINS in seen[script] and origins) or (ALL_ORIGINS in origins and seen[script])
            if overlap or wildcard_overlap:
                duplicate = sorted(overlap)[0] if overlap else ALL_ORIGINS
                raise ValueError(f'Page resource would be injected twice for {duplicate}')
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

    if any(ALL_ORIGINS in item['origins'] for item in use_orders):
        raise ValueError('Page resource order is cyclic for all-sites origin')

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
    development = development_configuration(root)
    development_origins = set(development['origins'])
    development_tools = set(development['tools'])
    result = json.loads(json.dumps(base))
    base_origins = list(result['allow_origins'])
    script_origins = [list(base_origins) for _ in result['page_scripts']]
    page_pack_origins = []
    declarations = {}
    use_orders = []
    any_enabled = False
    has_bridge_bindings = False
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
        features = manifest.get('features', [])
        if (type(features) is not list or len(features) > 32
                or any(type(feature) is not dict
                       or not {'id', 'label', 'value'} <= set(feature) <= {'id', 'label', 'value', 'folder'}
                       or type(feature['id']) is not str or not RESOURCE_ID.fullmatch(feature['id'])
                       or type(feature['label']) is not str or not 1 <= len(feature['label']) <= 80
                       or type(feature['value']) is not str or not 1 <= len(feature['value']) <= 160
                       or ('folder' in feature and not valid_feature_folder(feature['folder']))
                       for feature in features)
                or len({feature['id'] for feature in features}) != len(features)):
            raise ValueError(f'Enabled pack features are malformed: {pack_id}@{version}')
        roles = set(manifest['entrypoints'])
        if not roles or not roles <= {'page', 'reader', 'handler', 'command'}:
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
        granted_origins = set(grants.get('origins', []))
        if (not origin_subset(requested_origins, granted_origins)
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
        page_origins = list(requested_origins)
        if pack_id in development_tools and 'page' in roles:
            page_origins += sorted(development_origins - set(page_origins))
        page_pack_origins.append({'id': pack_id, 'version': version,
                                  'origins': page_origins,
                                  'features': json.loads(json.dumps(features))})
        if roles & {'page', 'handler'}:
            has_bridge_bindings = True
            for origin in requested_origins:
                exact_or_all_origin(origin)
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
        use_orders.append({'pack': pack_id, 'origins': tuple(page_origins),
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
                           'origins': set(page_origins),
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
            current['origins'].update(page_origins)
    if not any_enabled or not has_bridge_bindings:
        return base
    if declarations:
        for resource_id, origins in order_page_resources(declarations, use_orders):
            declaration = declarations[resource_id]
            result['page_scripts'].append(declaration['path'])
            script_origins.append(origins)
    configuration(result, script_origins)
    result['page_script_origins'] = script_origins
    result['page_pack_origins'] = page_pack_origins
    return result


def decision(config, origin):
    exact_origin(origin)
    reason = ('disabled' if not config['enabled'] else 'user_exclusion' if origin in config['exclude_origins']
              else 'all_sites_allow' if ALL_ORIGINS in config['allow_origins']
              else 'explicit_allow' if origin in config['allow_origins'] else 'not_allowed')
    return {'origin': origin, 'allowed': reason in ('explicit_allow', 'all_sites_allow'), 'reason': reason,
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
        self.page_pack_origins = config.get('page_pack_origins', []) if config else []
        self.component_token = None
        self.script_digests = []
        self.assets = {}
        self.asset_origins = {}
        self.ws_origins = None
        self.bootstrap_origins = set()
        self._profile_root = None
        self._plan_checked_at = 0.0
        self.development_origins = set()
        if config:
            self.bootstrap_origins.update(origin for origin in config['allow_origins']
                                          if origin == ALL_ORIGINS or origin not in config['exclude_origins'])
        if scripts is not None:
            self._publish_scripts(scripts, self.script_origins)

    def _publish_scripts(self, scripts, script_origins):
        """Publish plan scripts; retain prior digest→origin grants for old HTML."""
        digests = []
        assets = dict(self.assets)
        asset_origins = {digest: set(origins) for digest, origins in self.asset_origins.items()}
        for index, body in enumerate(scripts):
            digest = hashlib.sha256(body).hexdigest()
            assets[digest] = body
            digests.append(digest)
            if script_origins is None:
                granted = set(self.config['allow_origins']) if self.config else set()
            else:
                granted = set(script_origins[index])
            asset_origins.setdefault(digest, set()).update(granted)
        self.scripts = list(scripts)
        self.script_digests = digests
        self.script_origins = script_origins
        self.assets = assets
        self.asset_origins = asset_origins

    def _asset_allowed(self, digest, origin):
        granted = self.asset_origins.get(digest)
        return granted is not None and origin_allowed(origin, granted)

    def origin_digests(self, origin):
        seen = set()
        result = []
        for index, digest in enumerate(self.script_digests):
            if self.script_origins is not None and not origin_allowed(origin, self.script_origins[index]):
                continue
            if digest not in seen:
                seen.add(digest)
                result.append(digest)
        return result

    def page_plan(self, origin):
        access = 'current' if self.allowed(origin) else 'revoked'
        mode = 'development' if origin in self.development_origins else 'installed'
        scripts = self.origin_digests(origin) if access == 'current' else []
        packs = ([{'id': item['id'], 'version': item['version'],
                   'features': item.get('features', [])}
                  for item in self.page_pack_origins if origin_allowed(origin, item['origins'])]
                 if access == 'current' else [])
        revision = hashlib.sha256(json.dumps({'access': access, 'mode': mode, 'scripts': scripts, 'packs': packs},
                                             sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        return {'version': PLAN_VERSION, 'revision': revision, 'scripts': scripts,
                'packs': packs, 'access': access, 'mode': mode, 'application': 'reload'}

    def _read_ws_origins(self, profile, bridge=None):
        # Unmanaged profiles retain their external Hub contract. An enabled
        # managed browser/control plane exposes the local development channel on
        # every allowed origin, independently of installed handler bindings.
        if profile.get('components') is None:
            return None
        bridge = bridge or self.config
        if type(bridge) is dict and bridge.get('enabled'):
            excluded = set(bridge.get('exclude_origins') or profile.get('bridge', {}).get('exclude_origins') or [])
            return {origin for origin in bridge.get('allow_origins', [])
                    if origin == ALL_ORIGINS or origin not in excluded}
        try:
            runtime = read_json(self._profile_root / 'state/effective-runtime.json')
            components = runtime.get('components')
        except (FileNotFoundError, ValueError):
            components = profile.get('components')
        if type(components) is not dict or type(components.get('handlers')) is not dict:
            return set()
        return {origin for binding in components['handlers'].values()
                for origin in binding.get('origins', [])}

    def _write_runtime_state(self):
        if self._profile_root is None or self.config is None:
            return
        state = {'pid': os.getpid(), 'configuration': fingerprint(self.config),
                 'enabled': self.config['enabled']}
        path = self._profile_root / 'state/bridge.json'
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(state))
        temporary.chmod(0o600)
        temporary.replace(path)

    def _apply_plan(self, config):
        scripts = read_scripts(config) if config['enabled'] else []
        if self.config:
            self.bootstrap_origins.update(origin for origin in self.config['allow_origins']
                                          if origin == ALL_ORIGINS or origin not in self.config['exclude_origins'])
        self.config = config
        self.page_pack_origins = config.get('page_pack_origins', [])
        self.bootstrap_origins.update(origin for origin in config['allow_origins']
                                      if origin == ALL_ORIGINS or origin not in config['exclude_origins'])
        self._publish_scripts(scripts, config.get('page_script_origins'))
        self._write_runtime_state()

    def refresh_plan(self, force=False):
        """Re-read installed pack plan; keep the previous plan when refresh fails."""
        if self._profile_root is None:
            return False
        now = time.monotonic()
        if not force and now - self._plan_checked_at < PLAN_TTL_SECONDS:
            return False
        self._plan_checked_at = now
        try:
            profile = read_json(self._profile_root / 'profile.json')
            self.development_origins = set(development_configuration(self._profile_root)['origins'])
            base = configuration(profile['bridge'])
            plan = effective_configuration(self._profile_root, base)
            if profile.get('components') is not None:
                self.component_token = read_token(self._profile_root, 'component-token')
            else:
                self.component_token = None
            self.ws_origins = self._read_ws_origins(profile, plan)
            self.token = read_token(self._profile_root)
            previous = fingerprint(self.config) if self.config is not None else None
            self._apply_plan(plan)
            return fingerprint(plan) != previous
        except Exception as error:
            print(f'[tap bridge] plan refresh failed; keeping previous plan: {error}', flush=True)
            return False

    def load(self, loader):
        root = Path(os.environ['TAP_CORE_PROFILE'])
        self._profile_root = root
        profile = read_json(root / 'profile.json')
        self.development_origins = set(development_configuration(root)['origins'])
        self.config = effective_configuration(root, configuration(profile['bridge']))
        if profile.get('components') is not None:
            self.component_token = read_token(root, 'component-token')
        self.token = read_token(root)
        self.ws_origins = self._read_ws_origins(profile, self.config)
        self._apply_plan(self.config)
        self._plan_checked_at = time.monotonic()

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
        self.refresh_plan()
        request = flow.request
        origin = self.origin(request)
        allowed = self.allowed(origin)
        path = urlsplit(request.path)
        if not path.path.startswith(PREFIX):
            if allowed and request.headers.get('sec-fetch-dest', '') in ('', 'document', 'empty'):
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
        plan_channel = path.path == PLAN_PATH and origin_allowed(origin, self.bootstrap_origins)
        if (not allowed and not plan_channel) or len(supplied) != 1 or not hmac.compare_digest(supplied[0].encode(), self.token.encode()):
            self.reply(flow, 403, b'Bridge access denied')
            return
        ws = request.headers.get('upgrade', '').lower() == 'websocket'
        observed_origin = request.headers.get('origin')
        if (ws and observed_origin != origin) or (observed_origin and observed_origin != origin):
            self.reply(flow, 403, b'Bridge origin mismatch')
            return
        asset = ASSET_PATH.fullmatch(path.path)
        if asset:
            digest = asset.group(1)
            body = self.assets.get(digest)
            # Content-addressed bytes are retained after a plan swap, but only
            # origins that were granted that digest (now or previously) may fetch it.
            if body is None or not self._asset_allowed(digest, origin):
                self.reply(flow, 404)
                return
            self.reply(flow, 200, body, 'application/javascript; charset=utf-8')
            return
        if path.path == PLAN_PATH:
            self.reply(flow, 200, json.dumps(self.page_plan(origin)).encode(),
                       'application/json; charset=utf-8')
            return
        if path.path == RUNTIME_PATH:
            try:
                body = Path(__file__).with_name('page-runtime.js').read_bytes()
            except OSError:
                self.reply(flow, 503, b'Page runtime unavailable')
                return
            self.reply(flow, 200, body, 'application/javascript; charset=utf-8')
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
        self.refresh_plan()
        if 'text/html' not in response.headers.get('content-type', '').lower():
            return
        if flow.request.headers.get('sec-fetch-dest', '') not in ('', 'document', 'empty'):
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
        # Per-response authority goes only on TAP-owned script elements.
        # Site nonces, inline handlers, connections and other permissions stay intact.
        nonce = os.urandom(24).hex() if parsed.meta_policies or response.headers.get('content-security-policy') else parsed.nonce
        for start, end, policy in reversed(parsed.meta_policies):
            tag = '<meta http-equiv="Content-Security-Policy" content="' + html.escape(authorize_script_policy(policy, nonce), quote=True) + '">'
            body = body[:start] + tag + body[end:]
        if parsed.meta_policies:
            parsed = DocumentScripts(body)
        header = 'content-security-policy'
        if hasattr(response.headers, 'get_all'):
            policies = response.headers.get_all(header)
            if policies: response.headers.set_all(header, [authorize_script_policy(value, nonce) for value in policies])
        elif header in response.headers:
            response.headers[header] = authorize_script_policy(response.headers[header], nonce)
        nonce_attr = ' nonce="' + nonce + '"' if nonce else ''

        # Document <base> must never redirect token-bearing bootstrap/assets.
        asset_root = html.escape(self.origin(flow.request) + PREFIX, quote=True)
        plan = self.page_plan(origin)
        websocket_enabled = self.ws_origins is None or origin_allowed(origin, self.ws_origins)
        scripts = [f'<script id="{MARKER}"{nonce_attr} data-tap-token="{self.token}" '
                   f'data-tap-plan="{plan["revision"]}" data-tap-ws="{str(websocket_enabled).lower()}" '
                   f'data-tap-mode="{plan["mode"]}" '
                   f'src="{asset_root}runtime.js?token={self.token}"></script>']
        digests = plan['scripts']
        scripts += [f'<script{nonce_attr} src="{asset_root}core/{digest}.js?token={self.token}"></script>'
                    for digest in digests]
        position = parsed.body_end if parsed.body_end is not None else len(body)
        response.set_text(body[:position] + ''.join(scripts) + body[position:])
        for name in ('etag', 'last-modified'):
            response.headers.pop(name, None)
        response.headers['cache-control'] = 'no-store'


addons = [Bridge()]
