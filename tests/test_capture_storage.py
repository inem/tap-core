"""Synthetic storage faults; no live capture, OS routing or consumer protocol."""
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from tap_core.capture import Writer


def encoded(record):
    return (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")


class WrappedFile:
    def __init__(self, handle, **operations):
        self.handle = handle
        self.operations = operations

    def __getattr__(self, name):
        operation = self.operations.get(name)
        if operation is not None:
            return lambda *args, **kwargs: operation(self.handle, *args, **kwargs)
        return getattr(self.handle, name)


class StorageTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.stream = self.root / "stream.jsonl"

    def writer(self, **kwargs):
        writer = Writer(self.root, self.root, **kwargs)
        self.addCleanup(writer.close)
        return writer

    def finish(self, writer):
        writer.close(timeout=2)
        self.assertFalse(writer.thread.is_alive())
        self.assertEqual(writer.queue.unfinished_tasks, 0)
        self.assertEqual(writer.queued_bytes, 0)
        return json.loads((self.root / "capture.json").read_text())

    def records(self):
        archives = sorted(self.root.glob("stream.jsonl.[0-9]*"),
                          key=lambda path: int(path.suffix[1:]))
        paths = archives + ([self.stream] if self.stream.exists() else [])
        result = []
        for path in paths:
            data = path.read_bytes()
            self.assertTrue(not data or data.endswith(b"\n"), path.name)
            result.extend(json.loads(line) for line in data.splitlines())
        return result

    def wrap_stream(self, **operations):
        original = Path.open

        def open_file(path, *args, **kwargs):
            handle = original(path, *args, **kwargs)
            if path == self.stream and args and args[0] == "a+b":
                return WrappedFile(handle, **operations)
            return handle

        return patch.object(Path, "open", open_file)

    def test_restart_discards_only_incomplete_tail_even_without_new_traffic(self):
        records = [{"id": 1, "body": "строка\nещё"}, {"id": 2}]
        prefix = b"".join(map(encoded, records))
        tail = b'{"body":"' + "я".encode()[:1]
        self.stream.write_bytes(prefix + tail)
        (self.root / "read.offset").write_text("17")
        writer = self.writer()
        health = self.finish(writer)
        self.assertEqual(self.stream.read_bytes(), prefix)
        self.assertEqual(self.records(), records)
        self.assertEqual(health["written"], 0)
        self.assertEqual(health["dropped"], 1)
        self.assertEqual(health["write_errors"], 1)
        self.assertIn(f"discarded {len(tail)} bytes", health["last_error"])
        self.assertEqual((self.root / "read.offset").read_text(), "17")

    def test_restart_then_append_retains_complete_prefix(self):
        prefix = encoded({"id": 0})
        self.stream.write_bytes(prefix + b'{"id":')
        writer = self.writer()
        writer.submit({"id": 1})
        health = self.finish(writer)
        self.assertEqual(self.stream.read_bytes(), prefix + encoded({"id": 1}))
        self.assertEqual(health["written"], 1)
        self.assertEqual(health["dropped"], 1)

    def test_tail_repair_scans_large_tail_in_bounded_chunks(self):
        prefix = encoded({"id": 0})
        self.stream.write_bytes(prefix + b"x" * 200000)
        reads = []

        def read(handle, size):
            reads.append(size)
            return handle.read(size)

        with self.wrap_stream(read=read):
            writer = self.writer()
            self.finish(writer)
        self.assertGreater(len(reads), 3)
        self.assertLessEqual(max(reads), 65536)
        self.assertEqual(self.stream.read_bytes(), prefix)

    def test_file_with_no_complete_line_is_repaired_before_append(self):
        self.stream.write_bytes(b'{"id": 0}')  # No terminating newline.
        writer = self.writer()
        writer.submit({"id": 1})
        self.finish(writer)
        self.assertEqual(self.records(), [{"id": 1}])

    def test_short_recovery_read_refuses_to_truncate_or_append(self):
        original = encoded({"id": 0}) + b'{"id":'
        self.stream.write_bytes(original)
        with self.wrap_stream(read=lambda handle, size: handle.read(size)[:-1]):
            writer = self.writer()
            writer.submit({"id": 1})
            health = self.finish(writer)
        self.assertEqual(self.stream.read_bytes(), original)
        self.assertEqual(health["written"], 0)
        self.assertEqual(health["dropped"], 1)
        self.assertIn("short read", health["last_error"])

    def test_short_writes_are_completed_before_counting_record(self):
        with self.wrap_stream(write=lambda handle, data: handle.write(data[:3])):
            writer = self.writer()
            writer.submit({"body": "строка"})
            health = self.finish(writer)
        self.assertEqual(self.records(), [{"body": "строка"}])
        self.assertEqual(health["written"], 1)
        self.assertEqual(health["write_errors"], 0)

    def test_partial_write_error_cannot_poison_next_record(self):
        self.stream.write_bytes(encoded({"id": 0}))
        failed = False

        def write(handle, data):
            nonlocal failed
            if not failed:
                failed = True
                handle.write(data[:5])
                raise OSError("injected disk full after partial write")
            return handle.write(data)

        with self.wrap_stream(write=write):
            writer = self.writer()
            writer.submit({"id": 1})
            writer.submit({"id": 2})
            health = self.finish(writer)
        self.assertEqual(self.records(), [{"id": 0}, {"id": 2}])
        self.assertEqual(health["written"], 1)
        self.assertEqual(health["dropped"], 1)
        self.assertEqual(health["write_errors"], 1)

    def test_zero_progress_write_is_visible_and_next_record_recovers(self):
        calls = 0

        def write(handle, data):
            nonlocal calls
            calls += 1
            return 0 if calls == 1 else handle.write(data)

        with self.wrap_stream(write=write):
            writer = self.writer()
            writer.submit({"id": 0})
            writer.submit({"id": 1})
            health = self.finish(writer)
        self.assertEqual(self.records(), [{"id": 1}])
        self.assertEqual(health["dropped"], 1)
        self.assertIn("no progress", health["last_error"])

    def test_failed_rollback_blocks_later_appends_until_truncate_succeeds(self):
        # Exercise both a torn append and an ambiguous error after the complete
        # newline reached the file. In-process rollback uses the known offset.
        for complete in (False, True):
            with self.subTest(complete=complete):
                self.stream.write_bytes(encoded({"id": 0}))
                writes = 0
                truncates = 0

                def write(handle, data):
                    nonlocal writes
                    writes += 1
                    if writes == 1:
                        handle.write(data if complete else data[:5])
                        raise OSError("injected uncertain write")
                    return handle.write(data)

                def truncate(handle, size):
                    nonlocal truncates
                    truncates += 1
                    if truncates <= 2:
                        raise OSError("injected truncate failure")
                    return handle.truncate(size)

                with self.wrap_stream(write=write, truncate=truncate):
                    writer = self.writer()
                    for index in (1, 2, 3):
                        writer.submit({"id": index})
                    health = self.finish(writer)
                self.assertEqual(self.records(), [{"id": 0}, {"id": 3}])
                self.assertEqual(writes, 2)
                self.assertEqual(health["dropped"], 2)
                self.assertEqual(health["write_errors"], 2)

    def test_close_error_does_not_stop_later_records(self):
        writes = 0
        closes = 0

        def write(handle, data):
            nonlocal writes
            writes += 1
            if writes == 1:
                handle.write(data[:4])
                raise OSError("injected write failure")
            return handle.write(data)

        def close(handle):
            nonlocal closes
            closes += 1
            handle.close()
            if closes == 1:
                raise OSError("injected close failure")

        with self.wrap_stream(write=write, close=close):
            writer = self.writer()
            writer.submit({"id": 0})
            writer.submit({"id": 1})
            health = self.finish(writer)
        self.assertEqual(self.records(), [{"id": 1}])
        self.assertEqual(health["write_errors"], 2)
        self.assertEqual(health["dropped"], 1)

    def test_rotation_rename_failure_preserves_old_file_and_next_record_recovers(self):
        self.stream.write_bytes(encoded({"id": 0}))
        original = Path.rename
        failed = False

        def rename(path, target):
            nonlocal failed
            if path == self.stream and not failed:
                failed = True
                raise OSError("injected rename failure")
            return original(path, target)

        with patch.object(Path, "rename", rename):
            writer = self.writer(max_bytes=len(encoded({"id": 0})))
            writer.submit({"id": 1})
            writer.submit({"id": 2})
            health = self.finish(writer)
        self.assertEqual(self.records(), [{"id": 0}, {"id": 2}])
        self.assertEqual(health["dropped"], 1)
        self.assertEqual(health["write_errors"], 1)

    def test_rotation_prune_failure_after_rename_retries_before_next_append(self):
        old = self.root / "stream.jsonl.1"
        old.write_bytes(encoded({"id": -1}))
        self.stream.write_bytes(encoded({"id": 0}))
        original = Path.unlink
        failed = False

        def unlink(path, *args, **kwargs):
            nonlocal failed
            if path == old and not failed:
                failed = True
                raise OSError("injected retention failure")
            return original(path, *args, **kwargs)

        with patch.object(Path, "unlink", unlink):
            writer = self.writer(max_bytes=len(encoded({"id": 0})), keep_rolls=1)
            writer.submit({"id": 1})
            writer.submit({"id": 2})
            health = self.finish(writer)
        self.assertFalse(old.exists())
        self.assertEqual(self.records(), [{"id": 0}, {"id": 2}])
        self.assertEqual(health["dropped"], 1)
        self.assertEqual(health["write_errors"], 1)

    def test_persistent_prune_failure_stops_disk_growth(self):
        old = self.root / "stream.jsonl.1"
        old.write_bytes(encoded({"id": -1}))
        self.stream.write_bytes(encoded({"id": 0}))
        original = Path.unlink

        def unlink(path, *args, **kwargs):
            if path == old:
                raise OSError("injected persistent retention failure")
            return original(path, *args, **kwargs)

        with patch.object(Path, "unlink", unlink):
            writer = self.writer(max_bytes=len(encoded({"id": 0})), keep_rolls=1)
            for index in (1, 2, 3):
                writer.submit({"id": index})
            health = self.finish(writer)
        self.assertFalse(self.stream.exists())
        self.assertEqual(self.records(), [{"id": -1}, {"id": 0}])
        self.assertEqual(health["written"], 0)
        self.assertEqual(health["dropped"], 3)
        # A fresh writer completes interrupted retention before opening a file.
        restarted = self.writer(keep_rolls=1)
        restarted.submit({"id": 4})
        self.finish(restarted)
        self.assertEqual(self.records(), [{"id": 0}, {"id": 4}])

    def test_restart_finishes_retention_when_rename_left_no_current_file(self):
        for stamp in (1, 2, 3):
            (self.root / f"stream.jsonl.{stamp}").write_bytes(encoded({"id": stamp}))
        writer = self.writer(keep_rolls=2)
        self.finish(writer)
        self.assertEqual(self.records(), [{"id": 2}, {"id": 3}])

    def test_rotation_does_not_replace_archive_with_same_timestamp(self):
        archive = self.root / "stream.jsonl.123"
        archive.write_bytes(encoded({"id": -1}))
        self.stream.write_bytes(encoded({"id": 0}))
        with patch("tap_core.capture.time.time_ns", return_value=123):
            writer = self.writer(max_bytes=len(encoded({"id": 0})), keep_rolls=3)
            writer.submit({"id": 1})
            self.finish(writer)
        self.assertEqual(self.records(), [{"id": -1}, {"id": 0}, {"id": 1}])

    def test_backwards_clock_does_not_prune_the_newest_archive(self):
        (self.root / "stream.jsonl.1000").write_bytes(encoded({"id": -1}))
        self.stream.write_bytes(encoded({"id": 0}))
        with patch("tap_core.capture.time.time_ns", return_value=123):
            writer = self.writer(max_bytes=len(encoded({"id": 0})), keep_rolls=1)
            writer.submit({"id": 1})
            self.finish(writer)
        self.assertEqual(self.records(), [{"id": 0}, {"id": 1}])

    def test_transient_health_write_error_does_not_stop_capture(self):
        original = Path.replace
        failed = False

        def replace(path, target):
            nonlocal failed
            if path.name == "capture.tmp" and not failed:
                failed = True
                raise OSError("injected health write failure")
            return original(path, target)

        with patch.object(Path, "replace", replace):
            writer = self.writer()
            writer.submit({"id": 0})
            writer.submit({"id": 1})
            health = self.finish(writer)
        self.assertEqual(self.records(), [{"id": 0}, {"id": 1}])
        self.assertEqual(health["write_errors"], 1)
        self.assertIn("health", health["last_error"])

    def test_queue_overflow_and_shutdown_deadline_are_visible_and_bounded(self):
        entered = threading.Event()
        release = threading.Event()

        def write(handle, data):
            entered.set()
            if not release.wait(timeout=3):
                raise AssertionError("fixture was not released")
            return handle.write(data)

        with self.wrap_stream(write=write):
            writer = self.writer()
            # Release before cleanup joins, including when an assertion fails.
            self.addCleanup(release.set)
            writer.submit({"id": 0})
            self.assertTrue(entered.wait(timeout=2))
            for index in range(1, 66):
                writer.submit({"id": index})
            self.assertEqual(writer.dropped, 1)
            start = time.monotonic()
            writer.close(timeout=0.02)
            self.assertLess(time.monotonic() - start, 0.5)
            self.assertTrue(writer.thread.is_alive())
            self.assertEqual(writer.dropped, 65)
            writer.submit({"id": 66})
            self.assertEqual(writer.dropped, 66)
            release.set()
            health = self.finish(writer)
        self.assertEqual(self.records(), [{"id": 0}])
        self.assertEqual(health["dropped"], 66)
        self.assertEqual(health["write_errors"], 1)
        self.assertIn("shutdown timeout", health["last_error"])

    def test_queue_byte_budget_drop_is_present_in_health(self):
        writer = self.writer()
        writer.submit({"body": "x" * (3 * 1024 * 1024)})
        health = self.finish(writer)
        self.assertEqual(self.records(), [])
        self.assertEqual(health["dropped"], 1)
        self.assertEqual(health["written"], 0)


if __name__ == "__main__":
    unittest.main()
