"""Policy/credential/body safety at native hook boundaries, no network mutation."""
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from tap_core.bridge import Bridge, configuration, decision, read_json, read_scripts, read_token
from tap_core.runtime import Profile, MacOS, TapError
from tap_core.cli import main, bridge_status, doctor

TOKEN = 'a' * 48


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
        f = flow('/__tap/probe/core/0.js?token=' + TOKEN)
        self.bridge.requestheaders(f)
        self.assertEqual(f.response.content, b'window.fixture = true;')
        self.assertEqual(f.request.host, 'example.test')

    def test_nonce_bootstrap_order_idempotence_and_cache(self):
        f = flow(response=Response('<body><script nonce="YWJjZA==">0</script></body>'))
        self.bridge.response(f)
        once = f.response.body
        self.bridge.response(f)
        self.assertEqual(f.response.body, once)
        self.assertEqual(once.count('id="tap-probe-bootstrap"'), 1)
        self.assertEqual(once.count('nonce="YWJjZA=="'), 3)
        self.assertLess(once.index('runtime.js?'), once.index('core/0.js?'))
        self.assertNotIn('etag', f.response.headers)
        self.assertEqual(f.response.headers['cache-control'], 'no-store')

    def test_valid_nonce_attribute_forms_and_fake_script_text(self):
        for attr, value in (('nonce = "YWJjZA=="', 'YWJjZA=='), ("nonce = 'YWJjZA=='", 'YWJjZA=='),
                            ('nonce=YWJjZA', 'YWJjZA'), ('NONCE = YWJjZA', 'YWJjZA'),
                            ('nonce="YWJjZA&#61;&#61;"', 'YWJjZA==')):
            with self.subTest(attr=attr):
                f = flow(response=Response('<body><!-- <script nonce="wrong"> -->'
                                           '<script ' + attr + '>0</script></body>'))
                self.bridge.response(f)
                self.assertIn('id="tap-probe-bootstrap" nonce="' + value + '"', f.response.body)
                self.assertNotIn('nonce="wrong" data-tap-token', f.response.body)

    def test_malformed_declaration_skips_injection_and_preserves_response(self):
        body = '<body><![notvalid[example]]><script nonce="correct"></script></body>'
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
