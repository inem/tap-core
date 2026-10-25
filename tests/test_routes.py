"""Capture-side routing: matching, publish/notify/listen, and the capture writer hook."""
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest

from tap_core import routes
from tap_core.capture import Writer
from test_records_journal import capture_record


def record(url):
    value = capture_record()
    value['url'] = url
    return value


class MatchTests(unittest.TestCase):
    def test_origins_prefixes_and_wildcards(self):
        claude = record('https://claude.ai/api/organizations/1/usage?x=1')
        other = record('https://example.test/api/organizations/1')
        self.assertTrue(routes.record_matches(claude, {'https://claude.ai'}))
        self.assertFalse(routes.record_matches(other, {'https://claude.ai'}))
        self.assertTrue(routes.record_matches(other, {'*'}))
        self.assertTrue(routes.record_matches(other, None))
        prefixes = {'https://claude.ai': ('/api/organizations/',)}
        self.assertTrue(routes.record_matches(claude, {'https://claude.ai'}, prefixes))
        self.assertFalse(routes.record_matches(record('https://claude.ai/telemetry'),
                                               {'https://claude.ai'}, prefixes))
        self.assertFalse(routes.record_matches({'url': 7}, {'https://claude.ai'}))


class RoundTripTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir='/tmp')  # short enough for sun_path
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name)

    def listen(self, owner, targets):
        received, arrived = [], threading.Event()

        def on_targets(names):
            received.append(names)
            arrived.set()

        listener = routes.WakeListener(routes.socket_path(self.state, owner), on_targets).start()
        self.addCleanup(listener.close)
        routes.publish(self.state, owner, targets, listener.path)
        return listener, received, arrived

    def test_only_matching_targets_of_each_owner_are_notified(self):
        _, readers, readers_arrived = self.listen('components', {
            'reader:usage': {'origins': {'https://claude.ai'},
                             'path_prefixes': {'https://claude.ai': ['/api/']}},
            'reader:x': {'origins': {'https://x.com'}, 'path_prefixes': {}}})
        _, commands, _ = self.listen('background', {
            'command:gemini': {'origins': {'https://gemini.google.com'}, 'path_prefixes': {}}})
        table = routes.RouteTable(self.state)
        self.addCleanup(table.close)
        self.assertEqual(table.notify(record('https://claude.ai/telemetry')), 0)
        self.assertEqual(table.notify(record('https://claude.ai/api/organizations')), 1)
        self.assertTrue(readers_arrived.wait(2))
        self.assertEqual(readers, [['reader:usage']])
        time.sleep(0.2)
        self.assertEqual(commands, [])

    def test_routes_reload_when_republished_and_vanish_when_withdrawn(self):
        listener, received, arrived = self.listen('components', {
            'reader:a': {'origins': {'https://a.test'}, 'path_prefixes': {}}})
        table = routes.RouteTable(self.state, reload_seconds=0)
        self.addCleanup(table.close)
        self.assertEqual(table.notify(record('https://b.test/')), 0)
        time.sleep(0.01)
        routes.publish(self.state, 'components', {
            'reader:b': {'origins': {'https://b.test'}, 'path_prefixes': {}}}, listener.path)
        self.assertEqual(table.notify(record('https://b.test/')), 1)
        self.assertTrue(arrived.wait(2))
        self.assertEqual(received[-1], ['reader:b'])
        routes.withdraw(self.state, 'components')
        self.assertEqual(table.notify(record('https://b.test/')), 0)

    def test_notify_never_raises_when_the_consumer_is_gone(self):
        routes.publish(self.state, 'components', {'reader:a': {'origins': None, 'path_prefixes': {}}},
                       self.state / 'missing.sock')
        (self.state / 'routes' / 'broken.json').write_text('{not json')
        table = routes.RouteTable(self.state)
        self.addCleanup(table.close)
        self.assertEqual(table.notify(record('https://a.test/')), 0)
        self.assertEqual(table.failed, 1)

    def test_long_state_paths_get_a_private_short_socket_path(self):
        deep = self.state / ('d' * 120)
        deep.mkdir()
        path = routes.socket_path(deep, 'components')
        self.assertLessEqual(len(os.fsencode(str(path))), 104)
        self.assertEqual(oct(path.parent.stat().st_mode & 0o777), '0o700')

    def test_capture_writer_notifies_after_the_record_is_on_disk(self):
        data = self.state / 'data'
        data.mkdir()
        seen_on_disk = []

        class Probe:
            def notify(self, value):
                seen_on_disk.append(value['url'] in (data / 'stream.jsonl').read_text())

        writer = Writer(data, self.state)
        writer.routes = Probe()
        writer.submit(record('https://a.test/one'))
        writer.close()
        self.assertEqual(seen_on_disk, [True])


if __name__ == '__main__':
    unittest.main()
