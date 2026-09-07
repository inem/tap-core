"""Synthetic idempotent reader: delivery receipt and projection in one transaction."""
import json
import os
from pathlib import Path
import sqlite3
import sys
import time


def main():
    context = json.loads(os.environ['TAP_PACK_CONTEXT'])
    delivery = os.environ['TAP_READER_DELIVERY_ID']
    config = context['config']
    mode = config.get('fixture_mode', 'normal')
    record = json.loads(sys.stdin.readline())
    time.sleep(config.get('fixture_delay', 0))
    (Path(context['state_dir']) / 'worker.pid').write_text(str(os.getpid()))
    marker = Path(context['state_dir']) / ('attempt-' + delivery)
    first = not marker.exists()
    marker.write_text('attempted')
    if mode == 'fail_before_once' and first:
        sys.exit(3)
    if mode == 'flood':
        sys.stdout.write('x' * (2 * 1024 * 1024))
        return
    target = Path(context['output_dir']) / 'projection.sqlite3'
    with sqlite3.connect(target) as db:
        db.execute('CREATE TABLE IF NOT EXISTS deliveries (id TEXT PRIMARY KEY, body TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS latest (singleton INTEGER PRIMARY KEY CHECK(singleton=1), body TEXT NOT NULL)')
        body = record.get('body', '')
        fresh = db.execute('INSERT OR IGNORE INTO deliveries(id,body) VALUES(?,?)', (delivery, body)).rowcount
        if fresh:
            db.execute('INSERT OR REPLACE INTO latest(singleton,body) VALUES(1,?)', (body,))
    if mode == 'fail_after_once' and first:
        sys.exit(4)
    if mode == 'hang_after_once' and first:
        time.sleep(30)


if __name__ == '__main__':
    main()
