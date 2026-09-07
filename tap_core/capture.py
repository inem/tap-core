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
        self.thread = threading.Thread(target=self.run, daemon=True, name="tap-writer")
        self.thread.start()

    def submit(self, record):
        # Conservative estimate without JSON serialization or encoding in hooks.
        size = sum(len(value) * 6 if isinstance(value, str) else 64 for value in record.values()) + 1024
        with self.lock:
            if self.closed or self.queued_bytes + size > QUEUE_BYTES:
                self.dropped += 1
                return
            try:
                self.queue.put_nowait((record, size))
                self.queued_bytes += size
            except queue.Full:
                self.dropped += 1

    def metrics(self):
        values = {"pid": os.getpid(), "updated_at": time.time(), "writer_alive": not self.closed,
                  "written": self.written, "dropped": self.dropped, "write_errors": self.errors,
                  "last_error": self.last_error, "queued_bytes": self.queued_bytes}
        temporary = self.health.with_suffix(".tmp")
        temporary.write_text(json.dumps(values) + "\n")
        temporary.replace(self.health)

    def roll(self):
        archive = self.stream.with_name(self.stream.name + f".{time.time_ns()}")
        self.stream.rename(archive)
        archives = sorted(self.stream.parent.glob("stream.jsonl.*"), key=lambda p: p.name)
        for old in archives[:-self.keep_rolls] if self.keep_rolls else archives:
            old.unlink()

    def run(self):
        handle = None
        try:
            self.metrics()
            while True:
                try:
                    item = self.queue.get(timeout=0.5)
                except queue.Empty:
                    self.metrics()
                    continue
                if item is None:
                    self.queue.task_done()
                    break
                record, size = item
                try:
                    if handle is None:
                        handle = self.stream.open("a", encoding="utf-8")
                    line = json.dumps(record, ensure_ascii=False) + "\n"
                    if handle.tell() and handle.tell() + len(line.encode("utf-8")) > self.max_bytes:
                        handle.close()
                        handle = None
                        self.roll()
                        handle = self.stream.open("a", encoding="utf-8")
                    handle.write(line)
                    handle.flush()
                    self.written += 1
                except Exception as error:
                    self.errors += 1
                    self.last_error = str(error)
                    print(f"[tap] capture write failed: {error}", flush=True)
                    if handle:
                        handle.close()
                        handle = None
                finally:
                    with self.lock:
                        self.queued_bytes -= size
                    self.queue.task_done()
                self.metrics()
        except Exception as error:
            self.errors += 1
            self.last_error = str(error)
            print(f"[tap] writer stopped: {error}", flush=True)
        finally:
            if handle:
                handle.close()
            self.closed = True
            try:
                self.metrics()
            except OSError as error:
                print(f"[tap] cannot report writer health: {error}", flush=True)

    def close(self):
        if not self.thread.is_alive():
            return
        self.queue.put(None, timeout=5)
        self.thread.join(timeout=10)
        if self.thread.is_alive():
            print("[tap] writer did not drain within shutdown timeout", flush=True)


def wants_body(ctype):
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
