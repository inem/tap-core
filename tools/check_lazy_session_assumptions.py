#!/usr/bin/env python3
"""Characterize #263 assumptions using synthetic, temporary files only.

This is a research harness, not a session engine or a performance benchmark.
Oracles: SQLite SELECT for visibility, file metadata for change detection, and
instrumented existing Reader/Journal calls for current work. No workers, network,
launchd, live profiles or native client stores are used. See the migration plan.
"""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import platform
import sqlite3
import subprocess
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tap_core import journal, readers
from tap_core.runtime import Profile


def signature(path):
    stat = path.stat()
    return [stat.st_mtime_ns, stat.st_size]


def file_change_probe(root):
    directory = root / 'source'
    directory.mkdir()
    source = directory / 'session.jsonl'
    source.write_bytes(b'{"text":"A"}\n')
    directory_before = signature(directory)
    before = source.stat()
    source.write_bytes(b'{"text":"B"}\n')
    directory_after = signature(directory)
    # Reproduce an in-place rewrite with preserved size and timestamp.
    os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
    result = {
        'directory_signature_unchanged_after_child_rewrite': directory_before == directory_after,
        'file_mtime_size_unchanged_after_timestamp_restore': signature(source) == [before.st_mtime_ns, before.st_size],
        'content_changed': source.read_bytes() == b'{"text":"B"}\n',
    }
    assert all(result.values()), result
    return result


def wal_change_probe(root):
    path = root / 'native.sqlite3'
    with closing(sqlite3.connect(path)) as writer:
        assert writer.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
        writer.execute('PRAGMA wal_autocheckpoint=0')
        writer.execute('CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT)')
        writer.execute("INSERT INTO sessions VALUES ('one', 'before')")
        writer.commit()
        writer.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        main_before = signature(path)
        wal_before = signature(Path(str(path) + '-wal'))
        writer.execute("UPDATE sessions SET title='after' WHERE id='one'")
        writer.commit()
        # Keep writer open: closing the last connection may checkpoint the WAL.
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as reader:
            title = reader.execute("SELECT title FROM sessions WHERE id='one'").fetchone()[0]
        result = {
            'main_database_signature_unchanged': signature(path) == main_before,
            'wal_signature_changed': signature(Path(str(path) + '-wal')) != wal_before,
            'readonly_query_sees_committed_change': title == 'after',
        }
    assert all(result.values()), result
    return result


def encoded_records(count):
    # Existing v0 compatibility records; no production payloads or URLs.
    return b''.join((json.dumps({'url': 'https://unrelated.example/session',
                               'status': 200, 'body': str(i)}) + '\n').encode()
                    for i in range(count))


def reader_probe(root, count):
    profile = Profile(root / 'profile', '/fixture/mitmdump', 18999,
                      'explicit', 'http://fixture.example', [])
    profile.save()
    source = profile.root / 'data/stream.jsonl'
    source.write_bytes(b'')
    reader = readers.Reader(profile, 'research')
    spec = {'version': 1, 'revision': 'synthetic-research-1',
            'command': [sys.executable, '-c', 'raise SystemExit(99)'], 'config': {}}
    # Exclude first-use state creation from both measurements.
    reader.run(spec)

    def observe():
        with patch.object(readers, 'save', wraps=readers.save) as saves, \
                patch.object(os, 'chmod', wraps=os.chmod) as chmods, \
                patch.object(reader, 'execute', side_effect=AssertionError('unexpected worker')) as execute:
            result = reader.run(spec, max_records=count,
                                allowed_origins=frozenset({'https://selected.example'}))
        return {
            'records_advanced': result['completed_this_run'],
            'checkpoint_writes': sum(call.args[0] == reader.checkpoint for call in saves.call_args_list),
            'chmod_calls': chmods.call_count,
            'worker_invocations': execute.call_count,
        }

    source.write_bytes(encoded_records(count))
    filtered = observe()
    idle = observe()
    assert filtered['records_advanced'] == count, filtered
    assert idle['records_advanced'] == 0, idle
    assert filtered['worker_invocations'] == idle['worker_invocations'] == 0
    return {'filtered_batch': filtered, 'idle_tick': idle}


def journal_probe(root, count):
    data = root / 'journal'
    data.mkdir()
    (data / 'stream.jsonl').write_bytes(encoded_records(count))
    source = journal.Journal(data)
    with closing(source.scan()) as entries:
        last = None
        for entry in entries:
            last = entry.cursor
    read_lines = 0
    original_lines = source.lines

    def counted_lines(*args):
        nonlocal read_lines
        for line in original_lines(*args):
            read_lines += 1
            yield line

    with patch.object(source, 'lines', side_effect=counted_lines), \
            patch.object(journal, 'decode_record', wraps=journal.decode_record) as decode:
        with closing(source.scan(after=last)) as entries:
            resumed = list(entries)
    result = {
        'segment_records': count,
        'cursor_version': source.position(last)['v'],
        'idle_resume_records_returned': len(resumed),
        'idle_resume_lines_read': read_lines,
        'idle_resume_records_decoded': decode.call_count,
    }
    assert not resumed, result
    # Report the implementation's work, without freezing its present costs as
    # future regression expectations. Reader write counts are likewise observed.
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='tap-lazy-research-') as temporary:
        root = Path(temporary)
        result = {
            'schema': 'tap.lazy-session-research/v1',
            'evidence_level': 'synthetic characterization; not live or performance acceptance',
            'core_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
            'platform': platform.system(),
            'architecture': platform.machine(),
            'python': platform.python_version(),
            'sqlite': sqlite3.sqlite_version,
            'file_change': file_change_probe(root),
            'sqlite_wal': wal_change_probe(root),
            'reader': reader_probe(root, 50),
            'journal': journal_probe(root, 1000),
        }
    output = json.dumps(result, indent=2, sort_keys=True) + '\n'
    if args.output:
        args.output.write_text(output, encoding='utf-8')
    print(output, end='')


if __name__ == '__main__':
    main()
