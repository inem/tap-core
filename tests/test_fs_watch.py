"""Host change notification against real temporary files (macOS FSEvents, kqueue)."""
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

from tap_core.fs_watch import GrowthSignal, PathWatch, expand


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


class ExpandTests(unittest.TestCase):
    def test_home_absolute_and_profile_relative_paths(self):
        self.assertEqual(expand('~/x', '/profile'), os.path.realpath(str(Path.home() / 'x')))
        self.assertEqual(expand('/opt/x', '/profile'), os.path.realpath('/opt/x'))
        self.assertEqual(expand('data/readers/a', '/profile'), os.path.realpath('/profile/data/readers/a'))


@unittest.skipUnless(sys.platform == 'darwin', 'FSEvents and kqueue are macOS boundaries')
class WatchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(os.path.realpath(temporary.name))

    def test_path_watch_reports_changes_below_a_path_created_later_only(self):
        events = []
        changed = threading.Event()

        def on_change(paths):
            events.extend(paths)
            changed.set()

        watch = PathWatch([str(self.root / 'later/sub')], on_change).start()
        self.addCleanup(watch.close)
        time.sleep(0.2)
        (self.root / 'unrelated.txt').write_text('x')
        self.assertFalse(changed.wait(0.6), events)
        (self.root / 'later/sub').mkdir(parents=True)
        (self.root / 'later/sub/a.jsonl').write_text('x')
        self.assertTrue(changed.wait(5))
        self.assertTrue(all(path.startswith(str(self.root / 'later/sub')) for path in events), events)

    def test_watch_on_a_missing_path_reports_when_it_should_be_rerooted(self):
        watch = PathWatch([str(self.root / 'later/sub')], lambda paths: None).start()
        self.addCleanup(watch.close)
        self.assertEqual(watch.roots, (str(self.root),))
        self.assertFalse(watch.needs_reroot())
        (self.root / 'later/sub').mkdir(parents=True)
        self.assertTrue(watch.needs_reroot())

    def test_growth_signal_follows_appends_and_rotation_and_sleeps_when_idle(self):
        stream = self.root / 'stream.jsonl'
        stream.write_text('')
        growth = GrowthSignal(stream).start()
        self.addCleanup(growth.close)
        seen = growth.generation
        with stream.open('a') as handle:
            handle.write('1\n')
        self.assertNotEqual(growth.wait(seen, 5), seen)
        os.rename(stream, self.root / 'stream.jsonl.1')
        stream.write_text('')
        self.assertTrue(wait_for(lambda: growth.available))
        time.sleep(0.2)
        seen = growth.generation
        with stream.open('a') as handle:
            handle.write('2\n')  # the new file after rotation is watched too
        self.assertNotEqual(growth.wait(seen, 5), seen)
        time.sleep(0.2)
        seen = growth.generation
        started = time.monotonic()
        self.assertEqual(growth.wait(seen, 0.5), seen)
        self.assertGreaterEqual(time.monotonic() - started, 0.45)

    def test_growth_signal_creates_a_missing_data_directory(self):
        growth = GrowthSignal(self.root / 'data/stream.jsonl').start()
        self.addCleanup(growth.close)
        self.assertTrue(growth.available)
        seen = growth.generation
        (self.root / 'data/stream.jsonl').write_text('1\n')
        self.assertNotEqual(growth.wait(seen, 5), seen)

    def test_growth_signal_setup_errors_are_unavailable_not_crashes(self):
        from unittest.mock import patch
        from tap_core.fs_watch import WatchUnavailable
        growth = GrowthSignal(self.root / 'stream.jsonl')
        with patch('select.kqueue', side_effect=PermissionError('denied')):
            with self.assertRaises(WatchUnavailable):
                growth.start()
        self.assertFalse(growth.available)
        unreadable = GrowthSignal(self.root / 'stream.jsonl')
        real_open = os.open
        def refuse_file(path, flags, *args):
            if str(path).endswith('stream.jsonl'):
                raise PermissionError('denied')
            return real_open(path, flags, *args)
        with patch('os.open', side_effect=refuse_file):
            unreadable.start()
        self.addCleanup(unreadable.close)
        self.assertTrue(unreadable.available)
        self.assertIn('denied', unreadable.error)

    def test_growth_wait_returns_when_cancelled(self):
        growth = GrowthSignal(self.root / 'stream.jsonl').start()
        self.addCleanup(growth.close)
        stop = threading.Event()
        threading.Timer(0.1, stop.set).start()
        started = time.monotonic()
        growth.wait(growth.generation, 10, stop.is_set)
        self.assertLess(time.monotonic() - started, 2)


if __name__ == '__main__':
    unittest.main()
