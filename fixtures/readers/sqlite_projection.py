"""Synthetic reader: generation-scoped receipt and projection in one transaction."""
import json
import os
from pathlib import Path
import sqlite3
import sys
import time


def main():
    context = json.loads(os.environ['TAP_PACK_CONTEXT'])
    delivery = os.environ['TAP_READER_DELIVERY_ID']
    invocation = os.environ['TAP_READER_INVOCATION_ID']
    generation = context['reader_generation']
    config = context['config']
    mode = config.get('fixture_mode', 'normal')
    record = json.loads(sys.stdin.readline())
    time.sleep(config.get('fixture_delay', 0))
    (Path(context['state_dir']) / 'worker.pid').write_text(str(os.getpid()))
    marker = Path(context['state_dir']) / ('attempt-' + invocation)
    first = not marker.exists()
    marker.write_text('attempted')
    if mode == 'fail_before_once' and first:
        sys.exit(3)
    if mode == 'flood':
        sys.stdout.write('x' * (2 * 1024 * 1024))
        return
    target = Path(context['output_dir']) / 'projection.sqlite3'
    with sqlite3.connect(target) as db:
        # Include first-run schema creation in the transaction: a crash between
        # CREATEs must not leave a new store looking like the legacy schema.
        db.execute('BEGIN IMMEDIATE')
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'deliveries' in tables and 'receipts' not in tables:
            raise ValueError('Legacy fixture projection has unscoped receipts; use a new reader name or rebuild the derived store with explicit replay')
        db.execute('CREATE TABLE IF NOT EXISTS deliveries (id TEXT PRIMARY KEY, body TEXT NOT NULL)')
        if mode == 'fail_schema_once' and first:
            os._exit(5)
        db.execute('CREATE TABLE IF NOT EXISTS receipts (id TEXT PRIMARY KEY, generation INTEGER NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS latest (singleton INTEGER PRIMARY KEY CHECK(singleton=1), body TEXT NOT NULL)')
        body = record.get('body', '')
        if config.get('value_prefix'):
            value = json.loads(body)
            value['value'] = config['value_prefix'] + value['value']
            body = json.dumps(value)
        fresh = db.execute('INSERT OR IGNORE INTO receipts(id,generation) VALUES(?,?)', (invocation, generation)).rowcount
        if fresh:
            db.execute('INSERT INTO deliveries(id,body) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body', (delivery, body))
            db.execute('INSERT OR REPLACE INTO latest(singleton,body) VALUES(1,?)', (body,))
    if mode == 'fail_after_once' and first:
        sys.exit(4)
    if mode == 'hang_after_once' and first:
        time.sleep(30)


if __name__ == '__main__':
    main()
