"""Profile-scoped extraction of TAP capture: stream selectively, write off-hook.

No readers, site declarations, global paths or consumer offset mutations.
Loaded by mitmdump with explicit TAP_CORE_DATA and TAP_CORE_STATE directories.
"""
import json
import os
from pathlib import Path
import queue
import threading
import time

MAX_BYTES = 128 * 1024 * 1024
KEEP_ROLLS = 3
QUEUE_BYTES = 16 * 1024 * 1024


class Writer:
    def __init__(self, data, state, max_bytes=MAX_BYTES, keep_rolls=KEEP_ROLLS):
        self.stream = Path(data) / "stream.jsonl"
        self.health = Path(state) / "capture.json"
        self.max_bytes, self.keep_rolls = max_bytes, keep_rolls
        self.queue = queue.Queue(maxsize=64)
        self.lock = threading.Lock()
        self.queued_bytes = self.dropped = self.written = self.errors = 0
        self.last_error = None
        self.closed = False
        self.stopping = threading.Event()
        self.abandon = threading.Event()
        # A failed append must be rolled back before another record can follow it.
        self.rollback_offset = None
        self.thread = threading.Thread(target=self.run, daemon=True, name="tap-writer")
        self.thread.start()

    def submit(self, record):
        # Conservative estimate without JSON serialization or encoding in hooks.
        size = sum(len(value) * 6 if isinstance(value, str) else 64 for value in record.values()) + 1024
        with self.lock:
            if self.closed or self.stopping.is_set() or self.queued_bytes + size > QUEUE_BYTES:
                self.dropped += 1
                return
            try:
                self.queue.put_nowait((record, size))
                self.queued_bytes += size
            except queue.Full:
                self.dropped += 1

    def error(self, message, dropped=0):
        with self.lock:
            self.errors += 1
            self.last_error = message
            self.dropped += dropped
        print(f"[tap] {message}", flush=True)

    def metrics(self):
        with self.lock:
            values = {"pid": os.getpid(), "updated_at": time.time(), "writer_alive": not self.closed,
                      "written": self.written, "dropped": self.dropped, "write_errors": self.errors,
                      "last_error": self.last_error, "queued_bytes": self.queued_bytes}
        temporary = self.health.with_suffix(".tmp")
        temporary.write_text(json.dumps(values) + "\n")
        temporary.replace(self.health)

    def report(self):
        try:
            self.metrics()
        except Exception as error:
            # An unavailable health directory must not kill the capture worker.
            # The log remains the fallback until the next successful heartbeat.
            self.error(f"cannot report writer health: {error}")

    def archives(self):
        archives = [path for path in self.stream.parent.glob("stream.jsonl.*")
                    if path.name.removeprefix("stream.jsonl.").isdigit()]
        archives.sort(key=lambda path: int(path.name.removeprefix("stream.jsonl.")))
        return archives

    def prune(self):
        archives = self.archives()
        for old in archives[:-self.keep_rolls] if self.keep_rolls else archives:
            old.unlink()

    def roll(self):
        stamp = time.time_ns()
        archives = self.archives()
        if archives:
            # A clock adjustment must not make the newest archive the first
            # retention victim. Numeric archive names remain monotonically newer.
            stamp = max(stamp, int(archives[-1].name.removeprefix("stream.jsonl.")) + 1)
        archive = self.stream.with_name(self.stream.name + f".{stamp}")
        while archive.exists():
            stamp += 1
            archive = self.stream.with_name(self.stream.name + f".{stamp}")
        self.stream.rename(archive)
        self.prune()

    def open_stream(self):
        # Retry retention even after a restart between rename and pruning. Do not
        # keep accepting disk growth if removal of an old archive keeps failing.
        self.prune()
        handle = self.stream.open("a+b", buffering=0)
        try:
            if self.rollback_offset is not None:
                handle.truncate(self.rollback_offset)
                self.rollback_offset = None
            end = handle.seek(0, os.SEEK_END)
            position = end
            boundary = 0
            # Only the unfinished final line is repaired. Bounded chunks avoid
            # loading the journal or a potentially large damaged tail in memory.
            while position:
                start = max(0, position - 65536)
                handle.seek(start)
                block = handle.read(position - start)
                if len(block) != position - start:
                    raise OSError("short read while checking capture tail")
                newline = block.rfind(b"\n")
                if newline >= 0:
                    boundary = start + newline + 1
                    break
                position = start
            if boundary != end:
                handle.truncate(boundary)
                self.error(f"discarded {end - boundary} bytes of unfinished capture tail", dropped=1)
            handle.seek(0, os.SEEK_END)
            return handle
        except Exception:
            self.close_handle(handle)
            raise

    def close_handle(self, handle):
        if handle is not None:
            try:
                handle.close()
            except Exception as error:
                self.error(f"capture file close failed: {error}")

    def write(self, handle, line):
        self.rollback_offset = handle.seek(0, os.SEEK_END)
        try:
            remaining = memoryview(line)
            while remaining:
                count = handle.write(remaining)
                if count is None or count <= 0:
                    raise OSError("capture write made no progress")
                remaining = remaining[count:]
        except Exception:
            # Unbuffered I/O avoids a close() retry flushing a broken text buffer.
            # Keep the offset if truncate fails; every future open retries it.
            try:
                handle.truncate(self.rollback_offset)
                self.rollback_offset = None
            except OSError:
                pass
            raise
        self.rollback_offset = None
        with self.lock:
            self.written += 1

    def discard_pending(self):
        while True:
            try:
                _, size = self.queue.get_nowait()
            except queue.Empty:
                return
            with self.lock:
                self.queued_bytes -= size
                self.dropped += 1
            self.queue.task_done()

    def run(self):
        handle = None
        try:
            # Repair an existing tail even if no new traffic arrives. A missing
            # data directory is reported per submitted record, as before.
            if self.stream.exists():
                try:
                    handle = self.open_stream()
                except Exception as error:
                    self.error(f"capture recovery failed: {error}")
            else:
                try:
                    self.prune()
                except Exception as error:
                    self.error(f"capture retention failed: {error}")
            self.report()
            while not self.abandon.is_set():
                if self.stopping.is_set() and self.queue.empty():
                    break
                try:
                    record, size = self.queue.get(timeout=0.5)
                except queue.Empty:
                    self.report()
                    continue
                try:
                    if self.abandon.is_set():
                        with self.lock:
                            self.dropped += 1
                        continue
                    line = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
                    if handle is None:
                        handle = self.open_stream()
                    end = handle.seek(0, os.SEEK_END)
                    if end and end + len(line) > self.max_bytes:
                        self.close_handle(handle)
                        handle = None
                        self.roll()
                        handle = self.open_stream()
                    self.write(handle, line)
                except Exception as error:
                    self.error(f"capture write failed: {error}", dropped=1)
                    self.close_handle(handle)
                    handle = None
                finally:
                    with self.lock:
                        self.queued_bytes -= size
                    self.queue.task_done()
                self.report()
        except Exception as error:
            self.error(f"writer stopped: {error}")
        finally:
            with self.lock:
                self.stopping.set()
            self.discard_pending()
            self.close_handle(handle)
            with self.lock:
                self.closed = True
            self.report()

    def close(self, timeout=10):
        # No sentinel insertion: a full queue or blocked filesystem must not
        # prevent shutdown from returning within its drain budget.
        with self.lock:
            self.stopping.set()
        self.thread.join(timeout=timeout)
        if self.thread.is_alive():
            self.abandon.set()
            self.discard_pending()
            self.error("writer did not drain within shutdown timeout")


def wants_body(ctype):
    ctype = ctype.lower()
    return "event-stream" not in ctype and ("json" in ctype or ctype.startswith("text/"))


class Capture:
    def __init__(self, writer=None):
        self.writer = writer

    def load(self, loader):
        # Fail closed rather than falling back to the existing user's journal.
        if self.writer is None:
            self.writer = Writer(os.environ["TAP_CORE_DATA"], os.environ["TAP_CORE_STATE"])

    def responseheaders(self, flow):
        if flow.response and not wants_body(flow.response.headers.get("content-type", "")):
            flow.response.stream = True

    def response(self, flow):
        response = flow.response
        if response is None:
            return
        ctype = response.headers.get("content-type", "")
        streamed = bool(response.stream)
        keep = wants_body(ctype) and not streamed
        try:
            size = int(response.headers.get("content-length") or 0)
        except ValueError:
            size = 0
        if keep and not size:
            size = len(response.raw_content or b"")
        record = {"ts": time.time(), "method": flow.request.method, "url": flow.request.url,
                  "status": response.status_code, "ctype": ctype, "size": size,
                  "body_kept": keep, "streamed": streamed,
                  "ua": flow.request.headers.get("user-agent", "")}
        if keep:
            request_kept = not bool(flow.request.stream)
            record.update(body=response.get_text(strict=False), req_body_kept=request_kept,
                          req_body=(flow.request.get_text(strict=False) or "") if request_kept else "")
        self.writer.submit(record)

    def done(self):
        if self.writer:
            self.writer.close()


addons = [Capture()]
