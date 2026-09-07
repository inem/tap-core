"""Explicit, finite reader runs with independent progress and at-least-once input.

One JSONL record per child invocation; successful EOF/exit 0 is the boundary.
This is not the installed pack host or an external-action exactly-once engine.
"""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import tempfile
import time

from .journal import Journal, JournalGap
from .runtime import TapError, profile_lock

OUTPUT_LIMIT = 1024 * 1024


class ReaderError(TapError):
    pass


def private_dir(path):
    if path.is_symlink():
        raise ReaderError("Reader-owned directory must not be a symlink")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)


def save(path, value):
    # A unique temporary file also avoids following a stale fixed .tmp symlink.
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        try:
            json.dump(value, handle, ensure_ascii=False)
            handle.write('\n')
            handle.flush()
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def definition(path):
    value = json.loads(Path(path).read_text(encoding='utf-8'))
    if (not isinstance(value, dict) or set(value) != {'version', 'revision', 'command', 'config'}
            or type(value['version']) is not int or value['version'] != 1
            or not isinstance(value['revision'], str) or not value['revision']
            or not isinstance(value['config'], dict)
            or not isinstance(value['command'], list) or not value['command']
            or not all(isinstance(arg, str) and arg and '\0' not in arg for arg in value['command'])
            or not Path(value['command'][0]).is_absolute()):
        raise ReaderError('Invalid reader definition; expected version, revision, absolute command argv and config')
    return value


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


class Reader:
    def __init__(self, profile, name):
        if not re.fullmatch(r'[a-z][a-z0-9]*(?:[.-][a-z0-9]+)*', name) or len(name) > 100:
            raise ReaderError('Invalid reader name')
        self.profile, self.name = profile, name
        self.state = profile.root / 'state/readers' / name
        self.output = profile.root / 'data/readers' / name
        self.logs = profile.root / 'logs/readers' / name
        self.work = self.state / 'work'
        self.checkpoint = self.state / 'checkpoint.json'

    def prepare(self):
        for path in (self.state, self.output, self.logs):
            private_dir(path.parent)
            private_dir(path)
        private_dir(self.work)

    def load(self):
        try:
            if self.checkpoint.is_symlink():
                raise ReaderError('Reader checkpoint must not be a symlink')
            value = json.loads(self.checkpoint.read_text(encoding='utf-8'))
        except FileNotFoundError:
            return None
        required = {'version', 'definition', 'generation', 'cursor', 'processed', 'inflight', 'phase', 'error', 'updated_at'}
        if (not isinstance(value, dict) or set(value) != required
                or type(value['version']) is not int or value['version'] != 1
                or not isinstance(value['definition'], str) or not re.fullmatch('[0-9a-f]{64}', value['definition'])
                or type(value['generation']) is not int or value['generation'] < 1
                or type(value['processed']) is not int or value['processed'] < 0
                or (value['cursor'] is not None and not isinstance(value['cursor'], str))
                or (value['inflight'] is not None and not isinstance(value['inflight'], str))
                or value['phase'] not in ('ready', 'running', 'idle', 'failed', 'gap')
                or (value['error'] is not None and not isinstance(value['error'], str))
                or type(value['updated_at']) not in (int, float)):
            raise ReaderError('Invalid reader checkpoint; progress was not reset')
        return value

    def store(self, state, **changes):
        updated = dict(state, **changes, updated_at=time.time())
        save(self.checkpoint, updated)
        state.clear()
        state.update(updated)

    def initial(self, spec, generation=1):
        return {'version': 1, 'definition': fingerprint(spec), 'generation': generation,
                'cursor': None, 'processed': 0, 'inflight': None, 'phase': 'ready',
                'error': None, 'updated_at': time.time()}

    def status(self):
        state = self.load()
        # The phase is the last persisted observation, not a claim of liveness.
        return {'reader': self.name, 'configured': state is not None, 'checkpoint': str(self.checkpoint),
                'output': str(self.output), 'logs': str(self.logs), 'progress': state}

    def replay(self, spec):
        self.prepare()
        with profile_lock(self.state):
            previous = self.load()
            state = self.initial(spec, previous['generation'] + 1 if previous else 1)
            self.store(state)
        return self.status()

    def run(self, spec, max_records=100, timeout=30):
        if type(max_records) is not int or max_records < 1 or not 0 < timeout <= 300:
            raise ReaderError('Run requires positive max_records and timeout <= 300 seconds')
        self.prepare()
        with profile_lock(self.state) as lock:
            state = self.load()
            if state is None:
                state = self.initial(spec)
                self.store(state)
            if state['definition'] != fingerprint(spec):
                raise ReaderError('Reader definition changed; use a new reader name or explicit replay')
            completed = 0
            try:
                with closing(Journal(self.profile.root / 'data').scan(after=state['cursor'])) as entries:
                    for entry in entries:
                        delivery_id = hashlib.sha256(entry.cursor.encode()).hexdigest()
                        self.store(state, phase='running', inflight=delivery_id, error=None)
                        self.execute(spec, entry.record, delivery_id, timeout, lock)
                        self.store(state, cursor=entry.cursor, processed=state['processed'] + 1,
                                   inflight=None, phase='ready', error=None)
                        completed += 1
                        if completed == max_records:
                            break
                self.store(state, phase='idle', error=None)
            except Exception as error:
                # Keep the last acknowledged cursor, including after a timeout or
                # a crash between output and checkpoint. An explicit run retries.
                self.store(state, phase='gap' if isinstance(error, JournalGap) else 'failed',
                           error=type(error).__name__ + ': ' + str(error))
                raise
        return {'reader': self.name, 'completed_this_run': completed, 'progress': state}

    def execute(self, spec, record, delivery_id, timeout, lock):
        context = {'reader_id': self.name, 'config': spec['config'], 'state_dir': str(self.work),
                   'output_dir': str(self.output), 'log_dir': str(self.logs)}
        environment = {**os.environ, 'TAP_PACK_CONTEXT': json.dumps(context),
                       'TAP_READER_DELIVERY_ID': delivery_id, 'TAP_READER_LOCK_FD': str(lock.fileno())}
        output = {'stdout': bytearray(), 'stderr': bytearray()}
        process = None
        try:
            with tempfile.TemporaryFile(dir=self.state) as source, selectors.DefaultSelector() as selector:
                source.write((json.dumps(record, ensure_ascii=False) + '\n').encode())
                source.seek(0)
                process = subprocess.Popen(spec['command'], stdin=source, stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE, cwd=self.profile.root,
                                           env=environment, start_new_session=True, pass_fds=(lock.fileno(),))
                selector.register(process.stdout, selectors.EVENT_READ, 'stdout')
                selector.register(process.stderr, selectors.EVENT_READ, 'stderr')
                deadline = time.monotonic() + timeout
                while selector.get_map() or process.poll() is None:
                    if time.monotonic() >= deadline:
                        raise ReaderError('Reader timed out; completion is uncertain and cursor was not advanced')
                    for key, _ in selector.select(timeout=min(0.05, max(0, deadline - time.monotonic()))):
                        chunk = os.read(key.fd, 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        remaining = OUTPUT_LIMIT - sum(map(len, output.values()))
                        output[key.data].extend(chunk[:remaining])
                        if len(chunk) > remaining:
                            raise ReaderError('Reader output limit exceeded; cursor was not advanced')
                if process.returncode != 0:
                    raise ReaderError(f'Reader exited with code {process.returncode}; cursor was not advanced')
        finally:
            if process is not None:
                # Only this invocation's new process group; no shared TAP job.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=3)
                process.stdout.close()
                process.stderr.close()
            for name, contents in output.items():
                path = self.logs / ('last-' + name + '.log')
                with tempfile.NamedTemporaryFile(dir=self.logs, delete=False) as handle:
                    temporary = Path(handle.name)
                    try:
                        handle.write(contents)
                        handle.flush()
                        os.replace(temporary, path)
                    finally:
                        temporary.unlink(missing_ok=True)
