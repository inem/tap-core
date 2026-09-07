"""Profile-scoped extraction of TAP capture: stream selectively, write off-hook.

No readers, site declarations, global paths or consumer offset mutations.
Loaded by mitmdump with explicit TAP_CORE_DATA and TAP_CORE_STATE directories.
Optional TAP_CORE_CAPTURE JSON supplies profile storage limits (#8).
"""
import json
import os
from pathlib import Path
import queue
import threading
import time
import uuid

from .records import MAX_RECORD_BYTES

# Defaults match previously hardcoded writer/backend bounds.
# Distinct owners: backend stream cutoff ≠ retained body ≠ queue ≠ disk rolls.
# max_body_bytes default leaves room for request+response under journal MAX_RECORD_BYTES.
DEFAULT_CAPTURE = {
    "version": 1,
    "stream_large_bodies": 4 * 1024 * 1024,
    "segment_bytes": 128 * 1024 * 1024,
    "keep_rolls": 3,
    "queue_slots": 64,
    "queue_bytes": 16 * 1024 * 1024,
    "max_body_bytes": 12 * 1024 * 1024,
}

# Backward-compatible aliases used by older tests/docs.
MAX_BYTES = DEFAULT_CAPTURE["segment_bytes"]
KEEP_ROLLS = DEFAULT_CAPTURE["keep_rolls"]
QUEUE_BYTES = DEFAULT_CAPTURE["queue_bytes"]
_DECISION = "_tap_body_decision"


def mitm_size(nbytes):
    """Format bytes for mitmproxy --set size options (k/m/b)."""
    if type(nbytes) is not int or nbytes < 1:
        raise ValueError("size must be a positive int")
    if nbytes % (1024 * 1024) == 0:
        return f"{nbytes // (1024 * 1024)}m"
    if nbytes % 1024 == 0:
        return f"{nbytes // 1024}k"
    return f"{nbytes}b"


def capture_limits(value=None):
    """Validate profile capture limits; None → defaults."""
    if value is None:
        return dict(DEFAULT_CAPTURE)
    if type(value) is not dict or value.get("version") != 1:
        raise ValueError("capture limits require version 1 object")
    required = set(DEFAULT_CAPTURE)
    if set(value) != required:
        raise ValueError("capture limits keys must be exactly: " + ", ".join(sorted(required)))
    for name in ("stream_large_bodies", "segment_bytes", "queue_slots", "queue_bytes", "max_body_bytes"):
        number = value[name]
        if type(number) is not int or number < 1:
            raise ValueError(f"capture.{name} must be a positive int")
    if type(value["keep_rolls"]) is not int or value["keep_rolls"] < 0:
        raise ValueError("capture.keep_rolls must be a nonnegative int")
    if value["stream_large_bodies"] > 64 * 1024 * 1024:
        raise ValueError("capture.stream_large_bodies exceeds supported maximum (64 MiB)")
    if value["segment_bytes"] > 512 * 1024 * 1024:
        raise ValueError("capture.segment_bytes exceeds supported maximum (512 MiB)")
    if value["queue_bytes"] > 256 * 1024 * 1024:
        raise ValueError("capture.queue_bytes exceeds supported maximum (256 MiB)")
    if value["queue_slots"] > 4096:
        raise ValueError("capture.queue_slots exceeds supported maximum (4096)")
    if value["max_body_bytes"] > value["queue_bytes"]:
        raise ValueError("capture.max_body_bytes must not exceed queue_bytes")
    if value["max_body_bytes"] > 12 * 1024 * 1024:
        raise ValueError("capture.max_body_bytes exceeds supported maximum (12 MiB)")
    if 2 * value["max_body_bytes"] + 1024 * 1024 > MAX_RECORD_BYTES:
        raise ValueError("capture.max_body_bytes cannot fit request+response under journal record limit")
    return dict(value)


def limits_from_env():
    raw = os.environ.get("TAP_CORE_CAPTURE")
    if not raw:
        return capture_limits()
    try:
        return capture_limits(json.loads(raw))
    except (ValueError, json.JSONDecodeError) as error:
        raise SystemExit(f"tap-core: invalid TAP_CORE_CAPTURE: {error}") from error


class Writer:
    def __init__(self, data, state, max_bytes=None, keep_rolls=None, *,
                 queue_slots=None, queue_bytes=None, limits=None):
        limits = dict(limits or capture_limits())
        if max_bytes is not None:
            limits["segment_bytes"] = max_bytes
        if keep_rolls is not None:
            limits["keep_rolls"] = keep_rolls
        if queue_slots is not None:
            limits["queue_slots"] = queue_slots
        if queue_bytes is not None:
            limits["queue_bytes"] = queue_bytes
        limits = capture_limits(limits)
        self.limits = limits
        self.stream = Path(data) / "stream.jsonl"
        self.health = Path(state) / "capture.json"
        self.max_bytes = limits["segment_bytes"]
        self.keep_rolls = limits["keep_rolls"]
        self.queue_bytes_limit = limits["queue_bytes"]
        self.queue = queue.Queue(maxsize=limits["queue_slots"])
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
            if self.closed or self.stopping.is_set() or self.queued_bytes + size > self.queue_bytes_limit:
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
                    if len(line) > MAX_RECORD_BYTES:
                        self.error("capture record exceeds journal reader limit", dropped=1)
                        continue
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


def size_hint(response):
    try:
        declared = int(response.headers.get("content-length") or -1)
    except ValueError:
        declared = -1
    if declared > 0:
        return declared
    raw = getattr(response, "raw_content", None)
    if raw is not None:
        return len(raw)
    return 0


def decide_body(response, max_body_bytes):
    """Choose whether to retain a response body before decoding/accumulation.

    Missing Content-Length alone does not force streaming: small chunked bodies
    stay bufferable; mitmproxy stream_large_bodies bounds large unknown lengths.
    """
    ctype = response.headers.get("content-type", "")
    hint = size_hint(response)
    if not wants_body(ctype):
        return False, "media_type", True, hint
    if response.stream:
        return False, "streamed", False, hint
    try:
        declared = int(response.headers.get("content-length") or -1)
    except ValueError:
        declared = -1
    if declared > max_body_bytes:
        return False, "oversize", True, declared
    raw = getattr(response, "raw_content", None)
    if raw is not None and len(raw) > max_body_bytes:
        return False, "oversize", False, len(raw)
    if declared > 0:
        size = declared
    elif raw is not None:
        size = len(raw)
    else:
        size = 0
    return True, "retained", False, size


def encode_record(record):
    return (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")


def fit_record(record):
    """Omit bodies until the serialized line fits the journal reader limit."""
    line = encode_record(record)
    if len(line) <= MAX_RECORD_BYTES:
        return record
    record = dict(record)
    if record.get("req_body_kept"):
        record.pop("req_body", None)
        record["req_body_kept"] = False
        record["req_body_reason"] = "oversize_decoded"
        line = encode_record(record)
        if len(line) <= MAX_RECORD_BYTES:
            return record
    if record.get("body_kept"):
        record.pop("body", None)
        record["body_kept"] = False
        record["body_reason"] = "oversize_decoded"
        record["req_body_kept"] = False
        record["req_body_reason"] = "response_not_retained"
        record.pop("req_body", None)
    return record


class Capture:
    def __init__(self, writer=None, limits=None):
        self.writer = writer
        self.limits = limits

    def load(self, loader):
        # Fail closed rather than falling back to the existing user's journal.
        if self.limits is None:
            self.limits = limits_from_env()
        if self.writer is None:
            self.writer = Writer(os.environ["TAP_CORE_DATA"], os.environ["TAP_CORE_STATE"],
                                 limits=self.limits)

    def responseheaders(self, flow):
        response = flow.response
        if response is None:
            return
        limits = self.limits or limits_from_env()
        keep, reason, force_stream, size = decide_body(response, limits["max_body_bytes"])
        setattr(response, _DECISION, {"keep": keep, "reason": reason,
                                      "force_stream": force_stream, "size": size})
        if not keep and (force_stream or reason == "media_type"):
            response.stream = True

    def response(self, flow):
        response = flow.response
        if response is None:
            return
        limits = self.limits
        if limits is None and self.writer is not None:
            limits = getattr(self.writer, "limits", None)
        if limits is None:
            limits = limits_from_env()
        prior = getattr(response, _DECISION, None)
        keep, reason, _force_stream, size = decide_body(response, limits["max_body_bytes"])
        streamed = bool(response.stream)
        # Prefer the header-time capture decision when we ourselves forced streaming
        # (oversize/media_type); otherwise backend streaming would look like "streamed"
        # with size 0 and lose the known Content-Length / reason.
        if prior and prior.get("force_stream") and prior["reason"] in ("oversize", "media_type", "unbounded"):
            keep, reason, size = False, prior["reason"], prior["size"]
        elif prior and size <= 0 < prior["size"]:
            size = prior["size"]
        if streamed and reason == "retained":
            keep, reason = False, "streamed"
        body = None
        if keep:
            body = response.get_text(strict=False)
            if body is None:
                keep, reason = False, "unavailable"
            else:
                encoded = len(body.encode("utf-8"))
                if encoded > limits["max_body_bytes"]:
                    keep, reason, body = False, "oversize_decoded", None
                    size = encoded
                elif size <= 0:
                    size = encoded
        record = {"record_version": 1, "record_id": str(uuid.uuid4()),
                  "ts": time.time(), "method": flow.request.method, "url": flow.request.url,
                  "status": response.status_code, "ctype": response.headers.get("content-type", ""),
                  "size": max(0, size), "body_kept": keep, "body_reason": reason, "streamed": streamed,
                  "req_body_kept": False, "req_body_reason": "response_not_retained",
                  "ua": flow.request.headers.get("user-agent", "")}
        if keep:
            request_streamed = bool(flow.request.stream)
            request_body = None if request_streamed else flow.request.get_text(strict=False)
            request_kept = request_body is not None
            request_reason = "streamed" if request_streamed else "retained" if request_kept else "unavailable"
            if request_kept and len(request_body.encode("utf-8")) > limits["max_body_bytes"]:
                request_kept, request_reason, request_body = False, "oversize_decoded", None
            record.update(body=body, req_body_kept=request_kept, req_body_reason=request_reason)
            if request_kept:
                record["req_body"] = request_body
        self.writer.submit(fit_record(record))

    def done(self):
        if self.writer:
            self.writer.close()


addons = [Capture()]
