#!/usr/bin/env python3
"""Direct vs runner delivery parity on a fixed A→B→A corpus (#9).

Uses the SQLite projection fixture. Direct path invokes the same one-record
child contract as Reader.execute without advancing a host checkpoint. Runner
path uses the real CLI. Compares projection order/latest and records wall-clock
cost. Does not claim byte-for-byte stdout identity or batch-stdin pack parity.
"""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tap_core.capture import Writer
from tap_core.journal import Journal
from tap_core.readers import Reader, fingerprint
from tap_core.runtime import Profile, profile_lock

FIXTURE = ROOT / 'fixtures/readers/sqlite_projection.py'
CORPUS = ('A', 'B', 'A')


def projection(path):
    with sqlite3.connect(path) as db:
        deliveries = [json.loads(row[0])['value']
                      for row in db.execute('SELECT body FROM deliveries ORDER BY rowid')]
        latest = json.loads(db.execute('SELECT body FROM latest').fetchone()[0])['value']
        receipts = db.execute('SELECT count(*) FROM receipts').fetchone()[0]
    return {'deliveries': deliveries, 'latest': latest, 'receipts': receipts}


def direct_deliver(profile, spec, name='direct'):
    """One child per journal record; same IDs as the runner, no checkpoint."""
    reader = Reader(profile, name)
    reader.prepare()
    generation = 1
    started = time.perf_counter()
    with profile_lock(reader.state) as lock:
        with closing(Journal(profile.root / 'data').scan(after=None)) as entries:
            for entry in entries:
                delivery_id = hashlib.sha256(entry.cursor.encode()).hexdigest()
                invocation_id = fingerprint([name, generation, delivery_id])
                reader.execute(spec, entry.record, delivery_id, invocation_id, generation, 30, lock)
    elapsed_ms = (time.perf_counter() - started) * 1000
    return projection(reader.output / 'projection.sqlite3'), elapsed_ms


def runner_deliver(profile, definition, name='runner'):
    started = time.perf_counter()
    argv = [sys.executable, str(ROOT / 'tap'), '--profile', str(profile.root),
            'reader', 'run', name, '--definition', str(definition)]
    result = subprocess.run(argv, text=True, capture_output=True, timeout=30)
    if result.returncode:
        raise RuntimeError(result.stderr or result.stdout)
    elapsed_ms = (time.perf_counter() - started) * 1000
    summary = json.loads(result.stdout)
    assert summary['completed_this_run'] == len(CORPUS), summary
    path = profile.root / 'data/readers' / name / 'projection.sqlite3'
    return projection(path), elapsed_ms, summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='tap-reader-parity-') as directory:
        profile = Profile(Path(directory), '/unused/synthetic/backend', 18998, 'explicit',
                          'http://fixture.test', [])
        profile.save()
        base = json.loads((ROOT / 'fixtures/capture/v1.jsonl').read_text().splitlines()[0])
        writer = Writer(profile.root / 'data', profile.root / 'state')
        for value in CORPUS:
            writer.submit(dict(base, record_id=str(uuid.uuid4()), body=json.dumps({'value': value})))
        writer.close()
        assert writer.written == len(CORPUS)

        spec = {'version': 1, 'revision': 'parity-1',
                'command': [sys.executable, str(FIXTURE)], 'config': {}}
        definition = profile.root / 'reader.json'
        definition.write_text(json.dumps(spec))

        direct, direct_ms = direct_deliver(profile, spec)
        via_runner, runner_ms, summary = runner_deliver(profile, definition)

        order_equal = direct['deliveries'] == via_runner['deliveries'] == list(CORPUS)
        latest_equal = direct['latest'] == via_runner['latest'] == CORPUS[-1]
        projection_equal = order_equal and latest_equal and direct['receipts'] == via_runner['receipts'] == len(CORPUS)
        if not projection_equal:
            raise SystemExit(f'parity failed: direct={direct} runner={via_runner}')

        per_record = runner_ms / len(CORPUS)
        report = {
            'scope': 'synthetic Writer + SQLite fixture; no network or launchd',
            'corpus': 'a_b_a_n3',
            'records': len(CORPUS),
            'reader': str(FIXTURE.relative_to(ROOT)),
            'projection_equal': True,
            'order_equal': True,
            'latest_equal': True,
            'direct': direct,
            'runner': via_runner,
            'runner_completed_this_run': summary['completed_this_run'],
            'intentional_differences': [
                'Direct path here uses the same one-JSONL-per-process contract as Reader.execute; it does not exercise pack batch-stdin EOF over many lines.',
                'Only the runner persists checkpoint/cursor/generation, status phases, and TAP_READER_LOCK_FD lifecycle across a finite CLI run.',
                'Runner cwd is the profile root; pack fixture checks may use state_dir as cwd.',
                'JSON serialization of records is structural equality of the projection, not byte-for-byte stdout identity.',
                'Append-only pack readers may duplicate on retry; this SQLite fixture uses generation-scoped receipts.',
            ],
            'invocation_cost': {
                'n_records': len(CORPUS),
                'direct_wall_ms_total': round(direct_ms, 2),
                'runner_wall_ms_total': round(runner_ms, 2),
                'runner_wall_ms_per_record': round(per_record, 2),
                'model': 'one_process_per_record',
                'note': 'Wall time includes process startup on a tiny synthetic corpus; not a throughput SLA.',
                'next_threshold': 'Revisit long-lived workers if per-record startup dominates a declared larger corpus or live backlog (#32), not from this n=3 alone.',
            },
            'temporary_profile_removed': True,
        }
        rendered = json.dumps(report, indent=2) + '\n'
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered)
        print(rendered)


if __name__ == '__main__':
    main()
