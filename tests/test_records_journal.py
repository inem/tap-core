"""Version compatibility and saved positions across real writer rotation/recovery."""
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tap_core.capture import Capture, Writer
from tap_core.journal import Journal, JournalChanged, JournalError, JournalGap
from tap_core.records import RecordError, decode_record, validate_record

ROOT = Path(__file__).resolve().parents[1]


def encoded(record):
    return (json.dumps(record, ensure_ascii=False) + '\n').encode()


def capture_record(ctype='application/json', body='{"fixture":"new"}', streamed=False,
                   request_body='', request_streamed=False, size='1'):
    records = []
    response = SimpleNamespace(status_code=200, headers={'content-type': ctype, 'content-length': size},
                               stream=streamed, get_text=lambda **kw: body, raw_content=b'')
    request = SimpleNamespace(method='GET', url='https://fixture.example/data', headers={},
                              stream=request_streamed, get_text=lambda **kw: request_body)
    capture = Capture(SimpleNamespace(submit=records.append))
    flow = SimpleNamespace(request=request, response=response)
    capture.responseheaders(flow)
    capture.response(flow)
    return records[0]


class RecordTests(unittest.TestCase):
    def test_capture_roundtrip_and_empty_body_distinction(self):
        for body in ('', 'строка', None):
            with self.subTest(body=body):
                record = capture_record(body=body)
                self.assertEqual(decode_record(encoded(record), allow_legacy=False), record)
                self.assertEqual(record['body_kept'], body is not None)
                self.assertEqual(record['body_reason'], 'unavailable' if body is None else 'retained')
                self.assertEqual('body' in record, body is not None)

    def test_media_type_and_streamed_are_distinct_without_body_access(self):
        class Response:
            status_code = 200
            def __init__(self, ctype):
                self.headers = {'content-type': ctype, 'content-length': '20'}
                self.stream = True
            def get_text(self, **kwargs):
                raise AssertionError('streamed body accessed')
        for ctype, reason in [('text/event-stream', 'media_type'), ('application/octet-stream', 'media_type'),
                              ('Application/JSON', 'streamed')]:
            records = []
            flow = SimpleNamespace(response=Response(ctype), request=SimpleNamespace(
                method='GET', url='https://fixture.example/data', headers={}))
            Capture(SimpleNamespace(submit=records.append)).response(flow)
            record = validate_record(records[0])
            self.assertEqual(record['body_reason'], reason)
            self.assertEqual(record['req_body_reason'], 'response_not_retained')
            self.assertNotIn('body', record)
            self.assertNotIn('req_body', record)

    def test_request_body_dispositions(self):
        for body, streamed, reason in [('', False, 'retained'), (None, False, 'unavailable'),
                                       ('not accessed', True, 'streamed')]:
            record = validate_record(capture_record(request_body=body, request_streamed=streamed))
            self.assertEqual(record['req_body_reason'], reason)
            self.assertEqual('req_body' in record, reason == 'retained')

    def test_repeated_identical_responses_get_distinct_ids(self):
        one, two = capture_record(), capture_record()
        self.assertNotEqual(one['record_id'], two['record_id'])

    def test_legacy_fixtures_are_accepted_without_invented_metadata(self):
        for line in (ROOT / 'fixtures/packs/records.jsonl').read_bytes().splitlines():
            record = decode_record(line)
            self.assertNotIn('record_id', record)
            self.assertNotIn('body_reason', record)
            with self.assertRaisesRegex(RecordError, 'Unversioned'):
                decode_record(line, allow_legacy=False)

    def test_unsupported_versions_and_invalid_fields_fail_clearly(self):
        valid = capture_record()
        for field, value in [('record_version', True), ('record_version', 2), ('record_id', 'not-an-id'),
                             ('status', True), ('ts', float('inf')), ('size', -1), ('body_kept', 1),
                             ('body_reason', ['retained']), ('body', None), ('req_body_kept', False)]:
            with self.subTest(field=field, value=value):
                record = dict(valid, **{field: value})
                with self.assertRaises(RecordError):
                    validate_record(record)
        with self.assertRaises(RecordError):
            validate_record(dict(valid, streamed=True))
        record = capture_record(streamed=True)
        with self.assertRaises(RecordError):
            validate_record(dict(record, body='unexpected'))

    def test_bad_json_duplicate_keys_and_invalid_utf8_are_errors(self):
        for value in [b'[]', b'{', b'\xff\n', b'{"url":"a","url":"b","status":200}',
                      b'{"url":"a","status":200,"body":NaN}',
                      '{"url":"a","status":200}'.encode('utf-16')]:
            with self.subTest(value=value), self.assertRaises(RecordError):
                decode_record(value)

    def test_checked_in_v1_examples_match_validator(self):
        for line in (ROOT / 'fixtures/capture/v1.jsonl').read_bytes().splitlines():
            decode_record(line, allow_legacy=False)


class JournalTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.stream = self.root / 'stream.jsonl'
        self.journal = Journal(self.root)

    def write(self, *records):
        self.stream.write_bytes(b''.join(map(encoded, records)))

    def append_with_writer(self, *records, **settings):
        writer = Writer(self.root, self.root, **settings)
        for record in records:
            writer.submit(record)
        writer.close(timeout=2)
        self.assertFalse(writer.thread.is_alive())
        return writer

    def test_resume_after_rotation_and_restart(self):
        first, second, third = [capture_record(body=str(n)) for n in range(3)]
        self.append_with_writer(first)
        saved = list(self.journal.scan())[-1].cursor
        self.append_with_writer(second, max_bytes=1)
        self.append_with_writer(third, max_bytes=1)
        self.assertEqual([e.record for e in Journal(self.root).scan(saved)], [second, third])
        self.assertEqual([e.record for e in Journal(self.root).scan()], [first, second, third])

    def test_deleted_anchor_is_gap_instead_of_silent_replay(self):
        self.append_with_writer(capture_record())
        saved = list(self.journal.scan())[-1].cursor
        latest = capture_record()
        self.append_with_writer(latest, max_bytes=1, keep_rolls=0)
        with self.assertRaisesRegex(JournalGap, 'unavailable'):
            list(self.journal.scan(saved))
        self.assertEqual([e.record for e in self.journal.scan()], [latest])

    def test_empty_directory_with_cursor_is_gap(self):
        self.write(capture_record())
        saved = list(self.journal.scan())[0].cursor
        self.stream.unlink()
        with self.assertRaises(JournalGap):
            list(self.journal.scan(saved))
        self.assertEqual(list(self.journal.scan()), [])

    def test_incomplete_current_tail_is_not_acknowledged_and_later_completes(self):
        first, second = capture_record(), capture_record()
        self.write(first)
        with self.stream.open('ab') as handle:
            handle.write(encoded(second)[:-1])
        entries = list(self.journal.scan())
        self.assertEqual([e.record for e in entries], [first])
        with self.stream.open('ab') as handle:
            handle.write(b'\n')
        self.assertEqual([e.record for e in self.journal.scan(entries[-1].cursor)], [second])

    def test_writer_tail_repair_preserves_saved_position(self):
        first, second = capture_record(), capture_record()
        self.write(first)
        saved = list(self.journal.scan())[-1].cursor
        with self.stream.open('ab') as handle:
            handle.write(b'{"broken":')
        self.append_with_writer(second)
        self.assertEqual([e.record for e in self.journal.scan(saved)], [second])

    def test_retention_during_scan_keeps_pinned_records_available(self):
        first, second = capture_record(), capture_record()
        self.write(first, second)
        scan = self.journal.scan()
        self.addCleanup(scan.close)
        self.assertEqual(next(scan).record, first)
        self.stream.unlink()
        self.write(capture_record())
        self.assertEqual([e.record for e in scan], [second])

    def test_appended_records_wait_for_next_snapshot(self):
        first, second = capture_record(), capture_record()
        self.write(first)
        scan = self.journal.scan()
        self.addCleanup(scan.close)
        saved = next(scan).cursor
        with self.stream.open('ab') as handle:
            handle.write(encoded(second))
        self.assertEqual(list(scan), [])
        self.assertEqual([e.record for e in self.journal.scan(saved)], [second])

    def test_rotation_between_listing_and_open_is_retryable(self):
        self.write(capture_record())
        original = self.journal.paths
        calls = 0
        def paths():
            nonlocal calls
            calls += 1
            result = original()
            if calls == 1:
                self.stream.rename(self.root / 'stream.jsonl.1')
                self.write(capture_record())
            return result
        with patch.object(self.journal, 'paths', side_effect=paths):
            with self.assertRaises(JournalChanged):
                list(self.journal.scan())

    def test_replacement_with_same_names_is_retryable(self):
        self.write(capture_record())
        original = self.journal.paths
        calls = 0
        def paths():
            nonlocal calls
            calls += 1
            if calls == 2:
                self.stream.unlink()
                self.write(capture_record())
            return original()
        with patch.object(self.journal, 'paths', side_effect=paths):
            with self.assertRaises(JournalChanged):
                list(self.journal.scan())

    def test_truncated_or_modified_anchor_is_not_accepted(self):
        first, second = capture_record(), capture_record()
        self.write(first, second)
        saved = list(self.journal.scan())[-1].cursor
        self.write(first)
        with self.assertRaisesRegex(JournalGap, 'truncated'):
            list(self.journal.scan(saved))
        self.write(first, capture_record())
        with self.assertRaisesRegex(JournalGap, 'changed'):
            list(self.journal.scan(saved))

    def test_same_first_legacy_record_in_two_segments_is_ambiguous(self):
        record = {'url': 'https://fixture.example/data', 'status': 200}
        self.write(record)
        (self.root / 'stream.jsonl.1').write_bytes(encoded(record))
        with self.assertRaisesRegex(JournalGap, 'Ambiguous'):
            list(self.journal.scan())

    def test_bad_record_and_unfinished_archive_fail_without_advancing(self):
        self.write(capture_record())
        saved = list(self.journal.scan())[-1].cursor
        with self.stream.open('ab') as handle:
            handle.write(b'{"record_version": 99}\n')
        with self.assertRaises(RecordError):
            list(self.journal.scan(saved))
        (self.root / 'stream.jsonl.1').write_bytes(b'{')
        with self.assertRaisesRegex(RecordError, 'Unfinished'):
            list(self.journal.scan())

    def test_line_allocation_limit_is_enforced(self):
        self.write(capture_record(body='x' * 1000))
        with self.assertRaisesRegex(RecordError, 'allocation limit'):
            list(Journal(self.root, max_record_bytes=64).scan())

    def test_invalid_cross_profile_and_future_cursors_fail(self):
        self.write(capture_record())
        saved = list(self.journal.scan())[0].cursor
        other = self.root / 'other'
        other.mkdir()
        with self.assertRaises(JournalGap):
            list(Journal(other).scan(saved))
        value = json.loads(base64.urlsafe_b64decode(saved))
        for key, replacement in [('v', 99), ('v', True), ('line', 0), ('line', True)]:
            token = base64.urlsafe_b64encode(json.dumps(dict(value, **{key: replacement})).encode()).decode()
            with self.assertRaises(JournalError):
                list(self.journal.scan(token))
        for token in ['', '???']:
            with self.assertRaises(JournalError):
                list(self.journal.scan(token))

    def test_read_error_is_explicit_and_does_not_look_like_eof(self):
        handle = SimpleNamespace(readline=Mock(side_effect=OSError('fixture read failure')))
        with self.assertRaisesRegex(JournalError, 'Cannot read'):
            list(self.journal.lines(self.stream, handle, 20))

    def test_missing_directory_is_not_reported_as_empty_history(self):
        with self.assertRaises(JournalError):
            list(Journal(self.root / 'missing').scan())

    def test_excess_segments_and_symlinks_are_rejected(self):
        for index in range(65):
            (self.root / ('stream.jsonl.' + str(index))).write_bytes(b'')
        with self.assertRaisesRegex(JournalError, 'Too many'):
            list(self.journal.scan())
        for path in self.root.iterdir():
            path.unlink()
        target = self.root / 'target'
        target.write_bytes(encoded(capture_record()))
        self.stream.symlink_to(target)
        with self.assertRaisesRegex(JournalError, 'symlink'):
            list(self.journal.scan())

    def test_legacy_and_v1_reach_the_existing_pack_reader(self):
        legacy = {'url': 'https://fixture.example/data', 'status': 200, 'body': '{"fixture":"old"}'}
        new = capture_record()
        self.write(legacy)
        self.append_with_writer(new, max_bytes=1)
        records = [e.record for e in self.journal.scan()]
        context = {'config': {'prefix': 'compatibility'}, 'output_dir': str(self.root)}
        result = subprocess.run([sys.executable, str(ROOT / 'fixtures/packs/reader/reader.py')],
                                input=''.join(encoded(r).decode() for r in records), text=True, capture_output=True,
                                env={**os.environ, 'TAP_PACK_CONTEXT': json.dumps(context)}, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        values = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual([value['body']['fixture'] for value in values], ['old', 'new'])
        result = subprocess.run([sys.executable, str(ROOT / 'fixtures/packs/reader/reader.py')],
                                input=json.dumps(dict(new, record_version=99))+'\n', text=True, capture_output=True,
                                env={**os.environ, 'TAP_PACK_CONTEXT': json.dumps(context)}, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Unsupported capture record version', result.stderr)


if __name__ == '__main__':
    unittest.main()
