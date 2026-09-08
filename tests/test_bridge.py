"""Policy/credential/body safety at native hook boundaries, no network mutation."""
import hashlib
import json
import os
from pathlib import Path
import runpy
import shutil
from types import SimpleNamespace
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from tap_core.bridge import Bridge, configuration, decision, read_json, read_scripts, read_token
from tap_core.runtime import Profile, MacOS, TapError
from tap_core.cli import main, bridge_status, doctor

TOKEN = 'a' * 48


def digest(body):
    return hashlib.sha256(body if isinstance(body, bytes) else body.encode()).hexdigest()


def config(**extra):
    return dict({'version': 1, 'enabled': True, 'hub_port': 19002,
                 'allow_origins': ['https://example.test', 'https://second.test'],
                 'exclude_origins': ['https://second.test'], 'page_scripts': []}, **extra)


class Response:
    def __init__(self, body='<html><body>sample</body></html>', streamed=False):
        self.body, self.stream = body, streamed
        self.headers = {'content-type': 'text/html', 'etag': 'old'}
    def get_text(self, strict=False):
        if self.stream:
            raise AssertionError('Stream body read')
        return self.body
    def set_text(self, value):
        self.body = value


class TestBridge(Bridge):
    def reply(self, flow, status, body=b'', ctype='text/plain'):
        flow.response = SimpleNamespace(status_code=status, content=body)


def flow(path='/', host='example.test', headers=None, response=None):
    return SimpleNamespace(request=SimpleNamespace(scheme='https', host=host, port=443, path=path,
                           headers=headers or {}), response=response, metadata={})


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bridge = TestBridge(config(), TOKEN, [b'window.fixture = true;'])
        self.profile = Profile(self.root, '/fixture/backend', 19000, 'explicit', 'http://fixture.test', [], bridge=config())

    def test_user_exclusion_overrides_allow_and_explains_limited_scope(self):
        value = decision(config(), 'https://second.test')
        self.assertFalse(value['allowed'])
        self.assertEqual(value['reason'], 'user_exclusion')
        self.assertEqual(value['tls_policy'], 'unchanged')
        self.assertEqual(value['app_scope'], 'unsupported')
        self.assertEqual(decision(config(), 'https://example.test')['reason'], 'explicit_allow')
        self.assertEqual(decision(config(), 'https://other.test')['reason'], 'not_allowed')

    def test_unsupported_authority_passes_ordinary_traffic_but_denies_bridge(self):
        f = flow(host='::1', response=Response())
        before = f.response.body
        self.bridge.requestheaders(f)
        self.bridge.response(f)
        self.assertEqual(f.response.body, before)
        f = flow('/__tap/probe/ws?token=' + TOKEN, host='::1')
        self.bridge.requestheaders(f)
        self.assertEqual(f.response.status_code, 403)

    def test_config_rejects_ambiguous_unknown_and_wrong_types(self):
        for value in (config(version=True), config(hub_port=True), config(unknown=1),
                      config(allow_origins=['https://*.example.test']), config(allow_origins=['https://example.test/']),
                      config(allow_origins=['https://example.test:443']), config(page_scripts=['relative']),
                      config(allow_origins=['https://example.test'] * 2)):
            with self.assertRaises(ValueError):
                configuration(value)
        path = self.root / 'duplicate.json'
        path.write_text('{"enabled":true,"enabled":false}')
        with self.assertRaises(ValueError):
            read_json(path)

    def test_profile_persists_config_and_private_stable_token(self):
        self.profile.save()
        token = read_token(self.root)
        self.profile.save()
        self.assertEqual(read_token(self.root), token)
        self.assertEqual(Profile.load(self.root).bridge, config())
        self.assertNotIn(token, (self.root / 'profile.json').read_text())
        (self.root / 'state/bridge-token').chmod(0o644)
        with self.assertRaises(ValueError):
            self.profile.save()

    def test_token_failures_identify_the_file_without_exposing_contents(self):
        self.profile.save()
        for name in ('bridge-token', 'component-token'):
            path = self.root / 'state' / name
            path.unlink(missing_ok=True)
            with self.subTest(name=name, failure='missing'), self.assertRaisesRegex(ValueError, 'Missing ' + name):
                read_token(self.root, name)
            for failure, content, mode in [('permissions', b'a' * 48, 0o644),
                                           ('format', b'not-a-valid-secret', 0o600),
                                           ('encoding', b'\xff' * 48, 0o600)]:
                path.write_bytes(content)
                path.chmod(mode)
                with self.subTest(name=name, failure=failure), self.assertRaisesRegex(ValueError, name) as raised:
                    read_token(self.root, name)
                self.assertNotIn(content.decode('ascii', errors='replace'), str(raised.exception))
            path.unlink()
            path.symlink_to(self.root / 'missing-target')
            with self.subTest(name=name, failure='symlink'), self.assertRaisesRegex(ValueError, name + ' must be a private regular file'):
                read_token(self.root, name)
            path.unlink()
            path.mkdir(mode=0o700)
            with self.subTest(name=name, failure='directory'), self.assertRaisesRegex(ValueError, name + ' must be a private regular file'):
                read_token(self.root, name)
            path.rmdir()
            path.write_text('a' * 48)
            path.chmod(0o600)
            with self.subTest(name=name, failure='unreadable'), patch.object(Path, 'read_text', side_effect=PermissionError(13, 'Permission denied')):
                with self.assertRaisesRegex(ValueError, 'Cannot read ' + name):
                    read_token(self.root, name)

    def test_hub_cannot_route_back_into_profile_proxy(self):
        self.profile.bridge = config(hub_port=self.profile.port)
        with self.assertRaises(TapError):
            self.profile.save()
        self.assertFalse((self.root / 'profile.json').exists())

    def test_old_profile_without_bridge_still_loads(self):
        self.profile.bridge = None
        self.profile.save()
        path = self.root / 'profile.json'
        data = json.loads(path.read_text())
        data.pop('bridge')
        path.write_text(json.dumps(data))
        self.assertIsNone(Profile.load(self.root).bridge)

    def test_route_scrubs_credentials_and_authorizes_original_origin(self):
        f = flow('/__tap/probe/ws?token=' + TOKEN + '&page=one', headers={
            'upgrade': 'websocket', 'origin': 'https://example.test',
            'cookie': 'secret', 'authorization': 'secret', 'proxy-authorization': 'secret',
            'x-tap-probe-origin': 'https://spoof.test'})
        self.bridge.requestheaders(f)
        self.assertEqual((f.request.host, f.request.port), ('127.0.0.1', 19002))
        self.assertEqual(f.request.headers['x-tap-probe-origin'], 'https://example.test')
        self.assertEqual(f.request.path, '/__tap/probe/ws?page=one')
        for name in ('cookie', 'authorization', 'proxy-authorization'):
            self.assertNotIn(name, f.request.headers)
        self.bridge.request(f)
        self.assertEqual(f.request.headers['x-tap-probe-token'], TOKEN)

    def test_managed_route_replaces_forged_component_authority(self):
        self.bridge.component_token = 'b' * 48
        f = flow('/__tap/probe/ws?token=' + TOKEN, headers={
            'upgrade': 'websocket', 'origin': 'https://example.test',
            'x-tap-component-token': 'forged'})
        self.bridge.requestheaders(f)
        self.bridge.request(f)
        self.assertEqual(f.request.headers['x-tap-component-token'], 'b' * 48)

    def test_ordinary_site_headers_survive_but_denied_reserved_authority_is_removed(self):
        headers = {'x-tap-probe-token': 'site-value', 'x-tap-probe-origin': 'site-origin',
                   'x-tap-component-token': 'site-component', 'authorization': 'site-auth', 'cookie': 'site-cookie'}
        for host in ('example.test', 'second.test', 'other.test'):
            with self.subTest(host=host):
                ordinary = flow('/ordinary', host, dict(headers))
                self.bridge.requestheaders(ordinary)
                self.bridge.request(ordinary)
                self.assertEqual(ordinary.request.headers, headers)
                reserved = flow('/__tap/probe/ws?token=wrong', host, dict(headers))
                self.bridge.requestheaders(reserved)
                self.assertEqual(reserved.response.status_code, 403)
                for name in headers:
                    self.assertNotIn(name, reserved.request.headers)

    def test_denied_bad_duplicate_token_or_wrong_ws_origin_never_routes(self):
        for host, token, origin in [('second.test', TOKEN, 'https://second.test'),
                                     ('example.test', 'wrong', 'https://example.test'),
                                     ('example.test', TOKEN + '&token=' + TOKEN, 'https://example.test'),
                                     ('example.test', TOKEN, 'https://other.test'),
                                     ('example.test', TOKEN, '')]:
            f = flow('/__tap/probe/ws?token=' + token, host, {'upgrade': 'websocket', 'origin': origin})
            self.bridge.requestheaders(f)
            self.assertEqual(f.response.status_code, 403)
            self.assertEqual(f.request.host, host)
            self.assertNotIn('token=', f.request.path)

    def test_disabled_bridge_denies_existing_page_routes(self):
        self.bridge.config = config(enabled=False)
        f = flow('/__tap/probe/ws?token=' + TOKEN)
        self.bridge.requestheaders(f)
        self.assertEqual(f.response.status_code, 403)
        f = flow(response=Response())
        before = f.response.body
        self.bridge.response(f)
        self.assertEqual(f.response.body, before)

    def test_serves_configured_asset_without_forwarding_to_hub(self):
        asset = digest(b'window.fixture = true;')
        f = flow(f'/__tap/probe/core/{asset}.js?token=' + TOKEN)
        self.bridge.requestheaders(f)
        self.assertEqual(f.response.content, b'window.fixture = true;')
        self.assertEqual(f.request.host, 'example.test')

    def test_effective_script_plan_is_scoped_per_origin(self):
        effective = config(allow_origins=['https://example.test', 'https://third.test'],
                           exclude_origins=[], page_scripts=['/installed/one.js',
                                                            '/installed/two.js'])
        effective['page_script_origins'] = [['https://example.test'], ['https://third.test']]
        bridge = TestBridge(effective, TOKEN, [b'one', b'two'])
        first = flow(host='example.test', response=Response())
        bridge.response(first)
        self.assertIn(f'core/{digest(b"one")}.js', first.response.body)
        self.assertNotIn(f'core/{digest(b"two")}.js', first.response.body)
        second = flow(host='third.test', response=Response())
        bridge.response(second)
        self.assertNotIn(f'core/{digest(b"one")}.js', second.response.body)
        self.assertIn(f'core/{digest(b"two")}.js', second.response.body)
        denied_asset = flow(f'/__tap/probe/core/{digest(b"two")}.js?token=' + TOKEN, host='example.test')
        bridge.requestheaders(denied_asset)
        self.assertEqual(denied_asset.response.status_code, 404)

    def test_nonce_bootstrap_order_idempotence_and_cache(self):
        f = flow(response=Response('<body><script nonce="YWJjZA==">0</script></body>'))
        self.bridge.response(f)
        once = f.response.body
        self.bridge.response(f)
        self.assertEqual(f.response.body, once)
        self.assertEqual(once.count('id="tap-probe-bootstrap"'), 1)
        self.assertEqual(once.count('nonce="YWJjZA=="'), 3)
        self.assertLess(once.index('runtime.js?'), once.index(f'core/{digest(b"window.fixture = true;")}.js?'))
        self.assertNotIn('etag', f.response.headers)
        self.assertEqual(f.response.headers['cache-control'], 'no-store')

    def test_valid_nonce_attribute_forms_and_fake_script_text(self):
        for attr, value in (('nonce = "YWJjZA=="', 'YWJjZA=='), ("nonce = 'YWJjZA=='", 'YWJjZA=='),
                            ('nonce=YWJjZA', 'YWJjZA'), ('NONCE = YWJjZA', 'YWJjZA'),
                            ('nonce="YWJjZA&#61;&#61;"', 'YWJjZA=='), ('nonce=YWJjZA==', 'YWJjZA=='),
                            ('nonce="&#x41;&plus;&sol;&lowbar;&equals;"', 'A+/_=')):
            with self.subTest(attr=attr):
                f = flow(response=Response('<body><!-- <script nonce="wrong"> -->'
                                           '<script ' + attr + '>0</script></body>'))
                self.bridge.response(f)
                self.assertIn('id="tap-probe-bootstrap" nonce="' + value + '"', f.response.body)
                self.assertNotIn('nonce="wrong" data-tap-token', f.response.body)

    def test_addon_import_and_injection_without_html_parser(self):
        import builtins
        original = builtins.__import__
        def without_parser(name, *args, **kwargs):
            if name == 'html.parser':
                raise ModuleNotFoundError("No module named 'html.parser'")
            return original(name, *args, **kwargs)
        with patch('builtins.__import__', without_parser):
            addon = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'tap_core/bridge.py'))
            bridge = addon['Bridge'](config(), TOKEN, [])
            f = flow(response=Response('<body><script nonce = "YWJjZA=="></script></body>'))
            bridge.response(f)
        self.assertIn('id="tap-probe-bootstrap" nonce="YWJjZA=="', f.response.body)

    def test_text_only_content_and_comments_do_not_supply_nonce_marker_or_body_end(self):
        fake = '<script id="tap-probe-bootstrap" nonce="wrong"></scriptx></body>'
        for tag in ('script', 'style', 'textarea', 'title', 'xmp', 'iframe', 'noembed', 'noframes', 'noscript'):
            with self.subTest(tag=tag):
                body = ('<body><' + tag + '>' + fake + '</' + tag.upper() + ' \n>'
                        '<!-- ' + fake + ' --><script nonce="correct"></script></body>')
                f = flow(response=Response(body))
                self.bridge.response(f)
                insertion = '<script id="tap-probe-bootstrap" nonce="correct"'
                self.assertIn(insertion, f.response.body)
                self.assertTrue(f.response.body.startswith(body[:-7]))
                self.assertTrue(f.response.body.endswith('</body>'))

    def test_first_duplicate_attribute_wins_even_when_empty_or_valueless(self):
        for first in ('nonce', 'nonce=""', 'nonce="wrong!"'):
            with self.subTest(first=first):
                f = flow(response=Response('<body><script ' + first + ' NONCE="wrong"></script>'
                                           '<script nonce="correct"></script></body>'))
                self.bridge.response(f)
                self.assertIn('id="tap-probe-bootstrap" nonce="correct"', f.response.body)
        f = flow(response=Response('<body><script nonce=correct NONCE=wrong id=other id=tap-probe-bootstrap></script></body>'))
        self.bridge.response(f)
        self.assertIn('id="tap-probe-bootstrap" nonce="correct"', f.response.body)
        body = '<body><script id=tap-probe-bootstrap id=other></script></body>'
        f = flow(response=Response(body))
        self.bridge.response(f)
        self.assertEqual(f.response.body, body)

    def test_invalid_numeric_entities_cannot_become_valid_nonce_or_marker(self):
        f = flow(response=Response('<body><script nonce="wrong&#1;" id="tap-probe-bootstrap&#1;"></script>'
                                   '<script nonce="correct"></script></body>'))
        self.bridge.response(f)
        self.assertIn('id="tap-probe-bootstrap" nonce="correct"', f.response.body)

    def test_script_self_closing_syntax_still_skips_its_text(self):
        body = '<body><script /><script id=tap-probe-bootstrap nonce=wrong></script><script nonce=correct></script></body>'
        f = flow(response=Response(body))
        self.bridge.response(f)
        self.assertIn('id="tap-probe-bootstrap" nonce="correct"', f.response.body)

    def test_malformed_declaration_skips_injection_and_preserves_response(self):
        for body in ('<body><![notvalid[example]]><script nonce="correct"></script></body>',
                     '<body><script nonce="unfinished>', '<body><!-- unfinished',
                     '<body><textarea>unfinished', '<body><plaintext>text',
                     '<body><!--><script nonce=correct></script><!-- later --></body>',
                     '<body><!---><script nonce=correct></script><!-- later --></body>',
                     '<body><script><!--<script></script><script nonce=wrong></script>--></script>'
                     '<script nonce=correct></script></body>',
                     '<body><script nonce="' + 'A' * 4097 + '"></script></body>'):
            with self.subTest(body=body[:70]):
                f = flow(response=Response(body))
                headers = dict(f.response.headers)
                with patch('builtins.print') as diagnostic:
                    self.bridge.response(f)
                self.assertEqual(f.response.body, body)
                self.assertEqual(f.response.headers, headers)
                diagnostic.assert_called_once_with('[tap bridge] HTML parsing failed; injection skipped', flush=True)
        valid = flow(response=Response())
        self.bridge.response(valid)
        self.assertIn('id="tap-probe-bootstrap"', valid.response.body)

    def test_foreign_base_cannot_redirect_bootstrap_or_page_asset_urls(self):
        from html.parser import HTMLParser
        from urllib.parse import urljoin, urlsplit
        class Sources(HTMLParser):
            def __init__(self):
                super().__init__()
                self.sources = []
            def handle_starttag(self, tag, attrs):
                if tag == 'script' and 'src' in dict(attrs):
                    self.sources.append(dict(attrs)['src'])
        foreign = 'https://elsewhere.test/assets/'
        f = flow(response=Response('<head><base href="' + foreign + '"></head><body></body>'))
        self.bridge.response(f)
        parsed = Sources()
        parsed.feed(f.response.body)
        self.assertEqual(len(parsed.sources), 2)
        for source in parsed.sources:
            self.assertEqual(urlsplit(urljoin(foreign, source)).netloc, 'example.test')
            self.assertTrue(source.startswith('https://example.test/__tap/probe/'))

    def observation_adapter(self):
        adapter = Mock(spec=MacOS)
        adapter.service_loaded.return_value = True
        adapter.service_pid.return_value = os.getpid()
        adapter.owns_port.return_value = adapter.port_open.return_value = adapter.flows.return_value = True
        adapter.backend_version.return_value = '12.2.3'
        self.profile.save()
        (self.root / 'state/capture.json').write_text(json.dumps({
            'pid': os.getpid(), 'updated_at': time.time(), 'writer_alive': True,
            'written': 0, 'dropped': 0, 'write_errors': 0, 'last_error': None, 'queued_bytes': 0}))
        return adapter

    def test_bridge_read_error_is_unknown_and_preserves_other_observations(self):
        adapter = self.observation_adapter()
        read = Path.read_text
        def denied(path, *args, **kwargs):
            if path == self.profile.root / 'state/bridge.json':
                raise PermissionError('synthetic denied bridge observation')
            return read(path, *args, **kwargs)
        with patch.object(Path, 'read_text', denied):
            result = doctor(self.profile, adapter)
        self.assertIsNone(result['bridge']['healthy'])
        self.assertIn('bridge', result['inspection_errors'])
        self.assertTrue(result['capture']['healthy'])
        self.assertTrue(result['port_owned'])
        self.assertFalse(result['healthy'])

    def test_malformed_bridge_observations_are_unknown(self):
        adapter = self.observation_adapter()
        path = self.root / 'state/bridge.json'
        for raw in (b'{', b'\xff', b'null', b'[]', b'{}',
                    b'{"pid":true,"configuration":"bad","enabled":1}'):
            with self.subTest(raw=raw):
                path.write_bytes(raw)
                result = doctor(self.profile, adapter)
                self.assertIsNone(result['bridge']['healthy'])
                self.assertIn('bridge', result['inspection_errors'])
                self.assertTrue(result['capture']['healthy'])
                self.assertFalse(result['healthy'])

    def test_missing_bridge_state_is_known_absence(self):
        result = doctor(self.profile, self.observation_adapter())
        self.assertIs(result['bridge']['healthy'], False)
        self.assertNotIn('bridge', result['inspection_errors'])
        self.assertTrue(result['capture']['healthy'])
        self.assertFalse(result['healthy'])

    def test_stream_denied_and_subframe_bodies_remain_untouched(self):
        for f in (flow(response=Response(streamed=True)),
                  flow(host='second.test', response=Response()),
                  flow(headers={'sec-fetch-dest': 'iframe'}, response=Response())):
            before = f.response.body
            self.bridge.response(f)
            self.assertEqual(f.response.body, before)

    def test_startup_load_binds_profile_and_status_checks_real_pid(self):
        self.profile.save()
        bridge = Bridge()
        with patch.dict(os.environ, {'TAP_CORE_PROFILE': str(self.root)}):
            bridge.load(None)
        adapter = SimpleNamespace(service_pid=lambda p: os.getpid())
        self.assertTrue(bridge_status(self.profile, adapter)['healthy'])
        adapter.service_pid = lambda p: os.getpid() + 1
        self.assertFalse(bridge_status(self.profile, adapter)['healthy'])

    def test_script_read_is_bounded_before_profile_mutation(self):
        path = self.root / 'large.js'
        path.write_bytes(b'a' * (256 * 1024 + 1))
        self.profile.bridge = config(page_scripts=[str(path)])
        with self.assertRaises(ValueError):
            self.profile.save()
        self.assertFalse((self.root / 'profile.json').exists())

    def test_cli_rejects_live_configuration_and_writes_when_stopped(self):
        self.profile.save()
        path = self.root / 'new.json'
        path.write_text(json.dumps(config(enabled=False)))
        argv = ['--profile', str(self.root), 'bridge', 'configure', '--config', str(path)]
        with patch.object(MacOS, 'service_loaded', return_value=True):
            self.assertEqual(main(argv), 1)
        self.assertTrue(Profile.load(self.root).bridge['enabled'])
        with patch.object(MacOS, 'service_loaded', return_value=False):
            self.assertEqual(main(argv), 0)
        self.assertFalse(Profile.load(self.root).bridge['enabled'])

    def test_content_addressed_asset_survives_plan_swap(self):
        first = b'window.packA = true;'
        second = b'window.packB = true;'
        effective = config(allow_origins=['https://example.test', 'https://third.test'],
                           exclude_origins=[], page_scripts=['/a.js', '/b.js'])
        effective['page_script_origins'] = [['https://example.test'], ['https://third.test']]
        bridge = TestBridge(effective, TOKEN, [first, second])
        # Replace plan with only B for third.test; A retained for example.test only.
        bridge.config = effective
        bridge._publish_scripts([second], [['https://third.test']])
        old = digest(first)
        retained = flow(f'/__tap/probe/core/{old}.js?token=' + TOKEN, host='example.test')
        bridge.requestheaders(retained)
        self.assertEqual(retained.response.status_code, 200)
        self.assertEqual(retained.response.content, first)
        leaked = flow(f'/__tap/probe/core/{old}.js?token=' + TOKEN, host='third.test')
        bridge.requestheaders(leaked)
        self.assertEqual(leaked.response.status_code, 404)
        fresh = flow(host='third.test', response=Response('<html><body>next</body></html>'))
        bridge.response(fresh)
        self.assertIn(f'core/{digest(second)}.js', fresh.response.body)
        self.assertNotIn(f'core/{old}.js', fresh.response.body)

    def test_identical_bytes_are_fetchable_for_each_granted_origin(self):
        body = b'window.shared = true;'
        effective = config(allow_origins=['https://example.test', 'https://third.test'],
                           exclude_origins=[], page_scripts=['/one.js', '/two.js'])
        effective['page_script_origins'] = [['https://example.test'], ['https://third.test']]
        bridge = TestBridge(effective, TOKEN, [body, body])
        self.assertEqual(bridge.script_digests[0], bridge.script_digests[1])
        for host in ('example.test', 'third.test'):
            with self.subTest(host=host):
                page = flow(host=host, response=Response())
                bridge.response(page)
                self.assertIn(f'core/{digest(body)}.js', page.response.body)
                asset = flow(f'/__tap/probe/core/{digest(body)}.js?token=' + TOKEN, host=host)
                bridge.requestheaders(asset)
                self.assertEqual(asset.response.status_code, 200)
                self.assertEqual(asset.response.content, body)

    def test_handler_pack_enable_while_running_rolls_back_registry(self):
        import sys
        from tap_core.pack_store import PackStore, build_artifact
        linked = Path(__file__).resolve().parents[1] / 'fixtures/packs/installed-linked'
        components = {
            'version': 1, 'python': sys.executable, 'bun': '/usr/bin/true',
            'readers': {}, 'handlers': {},
        }
        self.profile.bridge = config(allow_origins=[], exclude_origins=[], page_scripts=[])
        self.profile.components = components
        self.profile.save()
        store = PackStore(self.root)
        artifact = self.root / 'linked.tap-pack'
        build_artifact(linked, artifact)
        store.install(artifact)
        argv = ['--profile', str(self.root), 'pack', 'enable', 'fixture.installed-linked',
                '--version', '0.1.0',
                '--grant-origin', 'https://fixture.example',
                '--grant-capability', 'page.inject',
                '--grant-capability', 'capture.read',
                '--grant-capability', 'bridge.handle']
        with patch.object(MacOS, 'service_loaded', side_effect=lambda target: True):
            self.assertEqual(main(argv), 1)
        record = store.load()['packs']['fixture.installed-linked']
        self.assertFalse(record['enabled'])
        projected = store.effective_components(components)
        self.assertNotIn('fixture.installed-linked', projected['readers'])
        self.assertNotIn('fixture.installed-linked', projected['handlers'])

    def test_handler_pack_add_while_running_never_publishes_enabled_plan(self):
        """First pack add must refuse before enabling; bridge must not observe the grant."""
        import copy
        import sys
        from tap_core.pack_store import PackStore, build_artifact
        linked = Path(__file__).resolve().parents[1] / 'fixtures/packs/installed-linked'
        components = {
            'version': 1, 'python': sys.executable, 'bun': '/usr/bin/true',
            'readers': {}, 'handlers': {},
        }
        self.profile.bridge = config(allow_origins=[], exclude_origins=[], page_scripts=[])
        self.profile.components = components
        self.profile.save()
        self.assertFalse((self.root / 'state/pack-registry.json').is_file())
        artifact = self.root / 'linked.tap-pack'
        build_artifact(linked, artifact)
        bridge = Bridge()
        with patch.dict(os.environ, {'TAP_CORE_PROFILE': str(self.root)}):
            bridge.load(None)
        self.assertFalse(bridge.allowed('https://fixture.example'))

        published_enabled = []

        def fake_add(profile_root, source, assume_yes=False, live=False):
            store = PackStore(profile_root)
            installed = store.install(artifact)
            enabled = store.enable('fixture.installed-linked', '0.1.0',
                                   origins=['https://fixture.example'],
                                   capabilities=['page.inject', 'capture.read', 'bridge.handle'],
                                   live=live)
            return {**installed, **enabled, 'source': source}

        original_save = PackStore.save

        def save_and_refresh(store, value):
            packs = value.get('packs') or {}
            linked_record = packs.get('fixture.installed-linked')
            if linked_record and linked_record.get('enabled'):
                published_enabled.append(copy.deepcopy(linked_record))
                original_save(store, value)
                bridge._plan_checked_at = 0
                bridge.refresh_plan(force=True)
                return
            return original_save(store, value)

        argv = ['--profile', str(self.root), 'pack', 'add', 'owner/linked-http', '--yes']
        with patch.object(MacOS, 'service_loaded', side_effect=lambda target: True), \
             patch('tap_core.pack_add.add', side_effect=fake_add), \
             patch.object(PackStore, 'save', save_and_refresh):
            self.assertEqual(main(argv), 1)
        self.assertEqual(published_enabled, [])
        store = PackStore(self.root)
        record = store.load()['packs']['fixture.installed-linked']
        self.assertFalse(record['enabled'])
        bridge._plan_checked_at = 0
        bridge.refresh_plan(force=True)
        self.assertFalse(bridge.allowed('https://fixture.example'))
        projected = store.effective_components(components)
        self.assertNotIn('fixture.installed-linked', projected['readers'])
        self.assertNotIn('fixture.installed-linked', projected['handlers'])

    def test_refused_handler_enable_never_observable_by_bridge_refresh(self):
        import copy
        import sys
        from tap_core.pack_store import PackStore, build_artifact
        linked = Path(__file__).resolve().parents[1] / 'fixtures/packs/installed-linked'
        components = {
            'version': 1, 'python': sys.executable, 'bun': '/usr/bin/true',
            'readers': {}, 'handlers': {},
        }
        self.profile.bridge = config(allow_origins=[], exclude_origins=[], page_scripts=[])
        self.profile.components = components
        self.profile.save()
        store = PackStore(self.root)
        artifact = self.root / 'linked.tap-pack'
        build_artifact(linked, artifact)
        store.install(artifact)
        bridge = Bridge()
        with patch.dict(os.environ, {'TAP_CORE_PROFILE': str(self.root)}):
            bridge.load(None)
        self.assertFalse(bridge.allowed('https://fixture.example'))
        published_enabled = []
        original_save = PackStore.save

        def save_and_refresh(store_self, value):
            packs = value.get('packs') or {}
            linked_record = packs.get('fixture.installed-linked')
            if linked_record and linked_record.get('enabled'):
                published_enabled.append(copy.deepcopy(linked_record))
                original_save(store_self, value)
                bridge._plan_checked_at = 0
                bridge.refresh_plan(force=True)
                return
            return original_save(store_self, value)

        argv = ['--profile', str(self.root), 'pack', 'enable', 'fixture.installed-linked',
                '--version', '0.1.0',
                '--grant-origin', 'https://fixture.example',
                '--grant-capability', 'page.inject',
                '--grant-capability', 'capture.read',
                '--grant-capability', 'bridge.handle']
        with patch.object(MacOS, 'service_loaded', side_effect=lambda target: True), \
             patch.object(PackStore, 'save', save_and_refresh):
            self.assertEqual(main(argv), 1)
        self.assertEqual(published_enabled, [])
        self.assertFalse(store.load()['packs']['fixture.installed-linked']['enabled'])
        bridge._plan_checked_at = 0
        bridge.refresh_plan(force=True)
        self.assertFalse(bridge.allowed('https://fixture.example'))

    def test_failed_plan_refresh_keeps_previous_assets(self):
        script = self.root / 'page.js'
        script.write_text('window.keep = true;\n')
        self.profile.bridge = config(page_scripts=[str(script)],
                                     allow_origins=['https://example.test'],
                                     exclude_origins=[])
        self.profile.save()
        bridge = Bridge()
        with patch.dict(os.environ, {'TAP_CORE_PROFILE': str(self.root)}):
            bridge.load(None)
        bridge.reply = TestBridge.reply.__get__(bridge, Bridge)
        before = list(bridge.script_digests)
        self.assertEqual(len(before), 1)
        with patch('tap_core.bridge.effective_configuration', side_effect=ValueError('broken pack')):
            bridge._plan_checked_at = 0
            self.assertFalse(bridge.refresh_plan(force=True))
        self.assertEqual(bridge.script_digests, before)
        asset = flow(f'/__tap/probe/core/{before[0]}.js?token=' + bridge.token)
        bridge.requestheaders(asset)
        self.assertEqual(asset.response.status_code, 200)
        self.assertEqual(asset.response.content, b'window.keep = true;\n')

    def test_plan_refresh_picks_up_enabled_pack_without_reload(self):
        from tap_core.pack_store import PackStore, build_artifact
        source = Path(__file__).resolve().parents[1] / 'fixtures/packs/installed-page'
        self.profile.bridge = config(allow_origins=[], exclude_origins=[], page_scripts=[])
        self.profile.save()
        store = PackStore(self.root)
        artifact = self.root / 'page.tap-pack'
        build_artifact(source, artifact)
        store.install(artifact)
        store.enable('fixture.installed-page', '0.1.0',
                     origins=['https://fixture.example'], capabilities=['page.inject'])
        bridge = Bridge()
        with patch.dict(os.environ, {'TAP_CORE_PROFILE': str(self.root)}):
            bridge.load(None)
        bridge.reply = TestBridge.reply.__get__(bridge, Bridge)
        first = flow(host='fixture.example', response=Response())
        bridge.response(first)
        self.assertIn('/__tap/probe/core/', first.response.body)
        digests_v1 = list(bridge.script_digests)
        self.assertTrue(digests_v1)

        v2 = self.root / 'page-v2'
        shutil.copytree(source, v2)
        manifest = json.loads((v2 / 'pack.json').read_text())
        manifest['version'] = '0.2.0'
        with (v2 / 'feature.js').open('a') as handle:
            handle.write('\nwindow.featureV2 = true;\n')
        feature = next(resource for resource in manifest['resources']
                       if resource['id'] == 'fixture.feature')
        feature['version'] = '0.2.0'
        feature['sha256'] = hashlib.sha256((v2 / 'feature.js').read_bytes()).hexdigest()
        feature['source_revision'] = 'sha256:' + feature['sha256']
        manifest['entrypoints']['page']['uses'][1]['version'] = '0.2.0'
        (v2 / 'pack.json').write_text(json.dumps(manifest, indent=2) + '\n')
        second = self.root / 'page-v2.tap-pack'
        build_artifact(v2, second)
        store.update(second)
        bridge._plan_checked_at = 0
        self.assertTrue(bridge.refresh_plan(force=True))
        digests_v2 = list(bridge.script_digests)
        self.assertNotEqual(digests_v1, digests_v2)
        fresh = flow(host='fixture.example', response=Response('<html><body>v2</body></html>'))
        bridge.response(fresh)
        for old in digests_v1:
            if old not in digests_v2:
                self.assertNotIn(f'core/{old}.js', fresh.response.body)
                retained = flow(f'/__tap/probe/core/{old}.js?token=' + bridge.token,
                                host='fixture.example')
                bridge.requestheaders(retained)
                self.assertEqual(retained.response.status_code, 200)
