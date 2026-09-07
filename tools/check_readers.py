#!/usr/bin/env python3
"""Synthetic Writer -> real reader CLI processes; no backend, network or launchd."""
import argparse
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tap_core.capture import Writer
from tap_core.runtime import Profile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='tap-readers-') as directory:
        profile = Profile(Path(directory), '/unused/synthetic/backend', 18999, 'explicit', 'http://fixture.test', [])
        profile.save()
        base = json.loads((ROOT / 'fixtures/capture/v1.jsonl').read_text().splitlines()[0])
        writer = Writer(profile.root / 'data', profile.root / 'state')
        for value in ('A', 'B', 'A'):
            writer.submit(dict(base, record_id=str(uuid.uuid4()), body=json.dumps({'value': value})))
        writer.close()
        assert writer.written == 3 and not writer.thread.is_alive()
        definition = profile.root / 'reader.json'
        definition.write_text(json.dumps({'version': 1, 'revision': 'fixture-1',
                              'command': [sys.executable, str(ROOT / 'fixtures/readers/sqlite_projection.py')],
                              'config': {}}))
        def cli(action, name, *extra):
            argv = [sys.executable, str(ROOT / 'tap'), '--profile', str(profile.root), 'reader', action, name]
            if action != 'status':
                argv += ['--definition', str(definition)]
            result = subprocess.run(argv + list(extra), text=True, capture_output=True, timeout=10)
            if result.returncode:
                raise RuntimeError(result.stderr)
            return json.loads(result.stdout)
        assert cli('run', 'slow', '--max-records', '1')['completed_this_run'] == 1
        assert cli('run', 'fast')['completed_this_run'] == 3
        fast_checkpoint = (profile.root / 'state/readers/fast/checkpoint.json').read_bytes()
        assert cli('run', 'slow')['completed_this_run'] == 2
        assert cli('run', 'slow')['completed_this_run'] == 0
        assert cli('replay', 'slow')['progress']['generation'] == 2
        assert cli('run', 'slow')['completed_this_run'] == 3
        assert (profile.root / 'state/readers/fast/checkpoint.json').read_bytes() == fast_checkpoint
        for name in ('slow', 'fast'):
            with sqlite3.connect(profile.root / 'data/readers' / name / 'projection.sqlite3') as db:
                assert db.execute('SELECT count(*) FROM deliveries').fetchone()[0] == 3
                assert json.loads(db.execute('SELECT body FROM latest').fetchone()[0]) == {'value': 'A'}
    report = {'scope': 'synthetic Writer and real CLI reader subprocesses; no network or launchd',
              'independent_progress': True, 'resume': True, 'explicit_replay': True,
              'projection_a_b_a': True, 'fixture_deduplicates_repeated_delivery': True,
              'temporary_profile_removed': True}
    rendered = json.dumps(report, indent=2) + '\n'
    if args.output:
        args.output.write_text(rendered)
    print(rendered)


if __name__ == '__main__':
    main()
