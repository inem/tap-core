#!/usr/bin/env python3
"""Direct vs runner delivery parity on a fixed A→B→A corpus (#9).

Uses the SQLite projection fixture. The direct baseline is an independent
subprocess host: it does not import or call Reader.execute, so shared delivery
bugs in the runner path remain visible. Runner path uses the real CLI.
Compares projection order/latest and records wall-clock cost with commit,
reader/corpus digests and measurement environment. Does not claim
byte-for-byte stdout identity or batch-stdin pack parity.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tap_core.capture import Writer
from tap_core.runtime import Profile

FIXTURE = ROOT / 'fixtures/readers/sqlite_projection.py'
CORPUS = ('A', 'B', 'A')


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def sha256_text(text):
    return hashlib.sha256(text.encode()).hexdigest()


def git_commit():
    result = subprocess.run(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'],
                            text=True, capture_output=True, check=False)
    if result.returncode:
        return None
    return result.stdout.strip()


def source_dirty():
    result = subprocess.run(['git', '-C', str(ROOT), 'status', '--porcelain'],
                            text=True, capture_output=True, check=False)
    return bool(result.stdout.strip()) if result.returncode == 0 else None


def corpus_bytes():
    """Stable source JSONL, shared as input data rather than delivery machinery."""
    base = json.loads((ROOT / 'fixtures/capture/v1.jsonl').read_text().splitlines()[0])
    return ''.join(json.dumps(dict(base,
        record_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f'tap-reader-parity-v1/{index}')),
        body=json.dumps({'value': value})), ensure_ascii=False) + '\n'
        for index, value in enumerate(CORPUS)).encode()


def measurement_environment():
    return {
        'python': sys.version.split()[0],
        'executable': sys.executable,
        'platform': platform.platform(),
        'system': platform.system(),
        'release': platform.release(),
        'machine': platform.machine(),
        'mac_ver': platform.mac_ver()[0] or None,
    }


def projection(path):
    with sqlite3.connect(path) as db:
        deliveries = [json.loads(row[0])['value']
                      for row in db.execute('SELECT body FROM deliveries ORDER BY rowid')]
        latest = json.loads(db.execute('SELECT body FROM latest').fetchone()[0])['value']
        receipts = db.execute('SELECT count(*) FROM receipts').fetchone()[0]
    return {'deliveries': deliveries, 'latest': latest, 'receipts': receipts}


def invoke_independent(command, record, context, delivery_id, invocation_id, cwd):
    """Minimal one-record host: stdin JSONL + env; no Reader.execute / lock / killpg."""
    environment = {**os.environ,
                   'TAP_PACK_CONTEXT': json.dumps(context, allow_nan=False),
                   'TAP_READER_DELIVERY_ID': delivery_id,
                   'TAP_READER_INVOCATION_ID': invocation_id}
    payload = (json.dumps(record, ensure_ascii=False) + '\n').encode()
    result = subprocess.run(command, input=payload, cwd=cwd, env=environment,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f'direct child exited {result.returncode}: '
                           f'{(result.stderr or result.stdout).decode(errors="replace")}')


def direct_deliver(profile, command, source, name='direct'):
    """Independent baseline: source JSONL + raw subprocess, no Writer/Journal/Reader."""
    root = profile.root
    state = root / 'state/readers' / name / 'work'
    output = root / 'data/readers' / name
    logs = root / 'logs/readers' / name
    for path in (state, output, logs):
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(0o700)
    generation = 1
    started = time.perf_counter()
    for payload in source.splitlines():
        record = json.loads(payload)
        # IDs are opaque to this fixture. Direct uses stable source identities;
        # the runner uses its own cursor and generation identities.
        delivery_id = hashlib.sha256(record['record_id'].encode()).hexdigest()
        invocation_id = hashlib.sha256(f'{name}/{generation}/{delivery_id}'.encode()).hexdigest()
        context = {'reader_id': name, 'reader_generation': generation, 'config': {},
                   'state_dir': str(state), 'output_dir': str(output), 'log_dir': str(logs)}
        invoke_independent(command, record, context, delivery_id, invocation_id, root)
    elapsed_ms = (time.perf_counter() - started) * 1000
    return projection(output / 'projection.sqlite3'), elapsed_ms


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
        source = corpus_bytes()
        writer = Writer(profile.root / 'data', profile.root / 'state')
        for payload in source.splitlines():
            writer.submit(json.loads(payload))
        writer.close()
        assert writer.written == len(CORPUS)
        stream_bytes = (profile.root / 'data/stream.jsonl').read_bytes()
        corpus_values = []
        for line in stream_bytes.splitlines():
            corpus_values.append(json.loads(json.loads(line)['body'])['value'])
        assert tuple(corpus_values) == CORPUS

        command = [sys.executable, str(FIXTURE)]
        spec = {'version': 1, 'revision': 'parity-1', 'command': command, 'config': {}}
        definition = profile.root / 'reader.json'
        definition.write_text(json.dumps(spec))

        direct, direct_ms = direct_deliver(profile, command, source)
        via_runner, runner_ms, summary = runner_deliver(profile, definition)

        order_equal = direct['deliveries'] == via_runner['deliveries'] == list(CORPUS)
        latest_equal = direct['latest'] == via_runner['latest'] == CORPUS[-1]
        projection_equal = order_equal and latest_equal and direct['receipts'] == via_runner['receipts'] == len(CORPUS)
        if not projection_equal:
            raise SystemExit(f'parity failed: direct={direct} runner={via_runner}')

        per_record = runner_ms / len(CORPUS)
        report = {
            'scope': 'synthetic Writer + SQLite fixture; no network or launchd',
            'commit': git_commit(),
            'measured_at': datetime.now(timezone.utc).isoformat(),
            'source_dirty': source_dirty(),
            'corpus': 'a_b_a_n3',
            'records': len(CORPUS),
            'reader': str(FIXTURE.relative_to(ROOT)),
            'digests': {
                'reader_sha256': sha256_file(FIXTURE),
                'source_jsonl_sha256': hashlib.sha256(source).hexdigest(),
                'corpus_stream_sha256': hashlib.sha256(stream_bytes).hexdigest(),
                'corpus_values_sha256': sha256_text(json.dumps(list(CORPUS), separators=(',', ':'))),
                'capture_template_sha256': sha256_file(ROOT / 'fixtures/capture/v1.jsonl'),
            },
            'environment': measurement_environment(),
            'baseline': {
                'direct': 'independent_subprocess',
                'runner': 'tap_reader_cli',
                'shares_reader_execute': False,
                'shares_journal_scan': False,
                'source': 'stable source JSONL before Writer',
            },
            'projection_equal': True,
            'order_equal': True,
            'latest_equal': True,
            'direct': direct,
            'runner': via_runner,
            'runner_completed_this_run': summary['completed_this_run'],
            'intentional_differences': [
                'Direct baseline is an independent subprocess host and does not call Reader.execute, so runner delivery bugs are not masked.',
                'Direct does not set TAP_READER_LOCK_FD, OUTPUT_LIMIT, killpg, guardian, or persist a checkpoint.',
                'Direct IDs derive from stable source record IDs; runner IDs derive from journal cursors and reader generations. Compare projected values/order/receipt count, not identity bytes.',
                'Neither path exercises pack batch-stdin EOF over many lines in one process.',
                'JSON projection equality is structural (order/latest/receipts), not byte-for-byte stdout identity.',
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
        }
    report['temporary_profile_removed'] = not profile.root.exists()
    rendered = json.dumps(report, indent=2) + '\n'
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered)
    print(rendered)


if __name__ == '__main__':
    main()
