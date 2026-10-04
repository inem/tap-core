"""Wire observation invariants, using synthetic data only."""
import base64
from pathlib import Path
import tempfile
from types import SimpleNamespace as Obj
import unittest
from unittest.mock import patch

from tap_core.live_capture import Observer, Publisher, FRAGMENT_BYTES, MAX_REQUEST_BYTES, validate_routes
from tap_core.capture import capture_limits, DEFAULT_CAPTURE


class LiveCaptureTests(unittest.TestCase):
    def setUp(self):
        self.records = []
        self.observer = Observer(Obj(submit=self.records.append), [{
            'origin': 'https://fixture.example', 'path_prefix': '/responses',
            'metadata_headers': ['x-fixture-thread']}])
        self.flow = Obj(metadata={}, request=Obj(
            url='https://fixture.example/responses?secret=must-not-publish',
            method='POST', headers={'authorization': 'private', 'cookie': 'private',
                                    'x-fixture-thread': 'synthetic-thread'},
            raw_content=b'{"input":"synthetic"}', stream=False),
            response=Obj(status_code=200, headers={}, stream=False))

    def test_opt_in_defaults_and_validation(self):
        self.assertEqual(capture_limits()['live_routes'], ())
        self.assertEqual(len(capture_limits({**DEFAULT_CAPTURE, 'live_routes': self.observer.routes})['live_routes']), 1)
        for route in [dict(origin='https://fixture.example/', path_prefix='/'),
                      dict(origin='https://fixture.example.evil/path', path_prefix='/'),
                      dict(origin='https://user:pass@fixture.example', path_prefix='/'),
                      dict(origin='https://fixture.example', path_prefix='invalid')]:
            with self.assertRaises(ValueError): validate_routes([route])
        for header in ['authorization', 'cookie', 'set-cookie', 'x-api-key', 'x-secret-token', 'INVALID']:
            with self.assertRaises(ValueError):
                validate_routes([dict(origin='https://fixture.example', path_prefix='/', metadata_headers=[header])])

    def test_identity_metadata_without_credential_or_query(self):
        self.observer.request(self.flow)
        first = self.records[0]
        self.assertEqual(first['metadata']['headers'], {'x-fixture-thread': 'synthetic-thread'})
        self.assertEqual(first['metadata']['path'], '/responses')
        self.assertNotIn('private', str(self.records))
        self.assertNotIn('must-not-publish', str(self.records))
        self.assertEqual(base64.b64decode(self.records[1]['data']), self.flow.request.raw_content)

    def test_unmatched_origin_query_cannot_enable_observation(self):
        self.flow.request.url = 'https://evil.example/?url=https://fixture.example/responses'
        self.observer.request(self.flow)
        self.observer.responseheaders(self.flow)
        self.assertEqual(self.records, [])
        self.assertFalse(self.flow.response.stream)

    def test_live_bytes_are_identical_and_not_decoded_in_hook(self):
        self.observer.request(self.flow)
        self.observer.responseheaders(self.flow)
        chunks = [b'data: ', 'А'.encode()[:1], 'А'.encode()[1:], b'\n\n', b'']
        with patch('tap_core.live_capture.json.dumps', side_effect=AssertionError('serialization in hook')):
            for chunk in chunks:
                self.assertIs(self.flow.response.stream(chunk), chunk)
        selected = [r for r in self.records if r['direction'] == 'response']
        self.assertEqual(selected[0]['metadata']['content_type'], '')
        self.assertEqual(selected[-1]['phase'], 'end')
        self.assertEqual(b''.join(base64.b64decode(r['data']) for r in selected if r['phase'] == 'data'), b''.join(chunks))
        self.assertEqual([r['sequence'] for r in self.records], list(range(1, len(self.records) + 1)))

    def test_preexisting_transform_and_iterable_preserved(self):
        self.observer.request(self.flow)
        self.flow.response.stream = lambda chunk: [chunk.upper()] if chunk else []
        self.observer.responseheaders(self.flow)
        self.assertEqual(list(self.flow.response.stream(b'hello')), [b'HELLO'])
        self.assertEqual(list(self.flow.response.stream(b'')), [])
        packet = next(r for r in self.records if r['phase'] == 'data' and r['direction'] == 'response')
        self.assertEqual(base64.b64decode(packet['data']), b'HELLO')
        self.assertEqual(self.records[-1]['phase'], 'end')

    def test_compression_metadata_keeps_wire_bytes(self):
        self.flow.response.headers = {'content-encoding': 'gzip'}
        self.observer.request(self.flow)
        self.observer.responseheaders(self.flow)
        compressed = b'\x1f\x8b\x00\xff'
        self.assertEqual(self.flow.response.stream(compressed), compressed)
        self.assertEqual(self.records[-2]['metadata']['content_encoding'], 'gzip')
        self.assertEqual(base64.b64decode(self.records[-1]['data']), compressed)

    def test_ws_both_directions_and_large_binary_reassemble(self):
        self.observer.request(self.flow)
        self.flow.response.status_code = 101
        self.observer.responseheaders(self.flow)
        self.assertFalse(self.flow.response.stream)
        self.observer.websocket_start(self.flow)
        data = b'\x00\xff' * FRAGMENT_BYTES
        self.flow.websocket = Obj(messages=[Obj(content=data, type=2, from_client=True)])
        self.observer.websocket_message(self.flow)
        packets = [r for r in self.records if r.get('message') == 1 and r['phase'] == 'data']
        self.assertEqual(len(packets), 2)
        self.assertEqual(b''.join(base64.b64decode(r['data']) for r in packets), data)
        self.assertTrue(all(r['direction'] == 'request' for r in packets))
        self.flow.websocket.messages.append(Obj(content=b'answer', type=1, from_client=False))
        self.observer.websocket_message(self.flow)
        self.assertEqual(self.records[-1]['message'], 2)
        self.observer.websocket_end(self.flow)
        self.assertEqual(self.records[-1]['phase'], 'end')
        self.assertNotIn('message', self.records[-1])

    def test_errors_and_parallel_flows_keep_distinct_ids(self):
        self.observer.request(self.flow)
        other = Obj(metadata={}, request=self.flow.request, response=self.flow.response)
        self.observer.request(other)
        self.assertNotEqual(self.observer.state(self.flow)['id'], self.observer.state(other)['id'])
        self.observer.error(self.flow)
        self.assertEqual(self.records[-1]['reason'], 'transport_error')

    def test_unavailable_and_oversize_request_are_explicit(self):
        for data, streaming, reason in [(None, False, 'body_unavailable'),
                                        (b'already-streamed', True, 'body_unavailable'),
                                        (b'x' * (MAX_REQUEST_BYTES + 1), False, 'observation_limit')]:
            self.records.clear()
            self.flow.request.raw_content, self.flow.request.stream = data, streaming
            self.observer.request(self.flow)
            self.assertFalse(self.records[0]['metadata']['body_available'])
            self.assertEqual([r['phase'] for r in self.records], ['start', 'error', 'end'])
            self.assertEqual(self.records[1]['reason'], reason)

    def test_shutdown_abandons_queue_and_rejects_late_hooks(self):
        with tempfile.TemporaryDirectory() as root, patch('tap_core.live_capture.threading.Thread.start'):
            (Path(root) / 'state').mkdir()
            publisher = Publisher(root, 19999, slots=2)
            publisher.submit({'synthetic': 1}); publisher.submit({'synthetic': 2})
            with patch.object(publisher.thread, 'join'):
                publisher.close()
            self.assertEqual(publisher.pending.qsize(), 0)
            self.assertFalse(publisher.submit({'late': 3}))
            self.assertEqual(publisher.unreported, 3)

    def test_full_queue_is_nonblocking_and_counted(self):
        with tempfile.TemporaryDirectory() as root, patch('tap_core.live_capture.threading.Thread.start'):
            publisher = Publisher(root, 19999, slots=1)
            self.assertTrue(publisher.submit({'synthetic': 1}))
            self.assertFalse(publisher.submit({'synthetic': 2}))
            self.assertEqual((publisher.dropped, publisher.unreported), (1, 1))
            self.assertEqual(publisher.pending.qsize(), 1)


if __name__ == '__main__':
    unittest.main()
