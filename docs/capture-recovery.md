# Capture writer recovery

This is a bounded implementation slice for #8, built on the profile runtime in
#20. It does not finish #8 or define the reader/replay contract in #9. The record
fields, `data/stream.jsonl` path, numeric archive names and existing health keys
are unchanged. No consumer offset is read or written.

## Completed records and unfinished tails

The writer serializes one UTF-8 JSON value followed by a newline. A complete line
is the local recovery boundary. Before appending to an existing current file, it
scans backward in chunks of at most 64 KiB and truncates only the bytes after the
last newline. If the file has no newline, the whole unfinished file is removed
by truncation. This also discards a valid JSON value missing its final newline;
it does not invent a completion marker. Repair runs on startup even without new
traffic. The complete prefix is preserved byte for byte.

A repaired final fragment increments `dropped` once and `write_errors` once. Its
discarded byte count is logged and included in `last_error`. This counts one
unfinished line, not a claim about the number of network requests lost before
restart. Earlier process counters are not recovered from the health file.

The writer does not validate every previous line or repair corrupted archives.
External modification, multiple writers sharing one profile and corruption
inside a newline-terminated record are outside this recovery rule. If the tail
cannot be read completely or truncated, new records are rejected with visible
errors until a later attempt can repair it.

## Partial writes and subsequent traffic

Records use unbuffered binary file writes on the worker thread. A short write is
completed in a loop; a zero-progress result is an error. On a write exception,
the writer truncates back to the known offset before that record. If truncation
fails, the offset is retained in memory and every reopen must successfully
truncate to it before any later append. A record rejected while recovery is
unavailable increments both `dropped` and `write_errors`.

This prevents a failed append from silently joining onto a later JSON value.
Unbuffered I/O also avoids a text buffer retrying damaged output during close.
Close failures are logged and counted; they do not turn the original write error
into a silent worker exit.

`written` means that all serialized bytes were accepted by the file write calls.
It does **not** mean that they reached durable storage. There is no per-record
`fsync`, directory `fsync`, cross-process acknowledgment or exactly-once promise.
An I/O error may occur after bytes reached the file. While the same writer lives,
the saved offset allows rollback even if those bytes include a full newline. If
the process dies before rollback, restart can only distinguish a torn final line
from a complete one; a complete line may remain despite no successful write
being counted by the previous process.

## Rotation and retention

Rotation closes the current handle, renames the complete current file to a
numeric archive, then removes archives beyond `keep_rolls`. New names advance
beyond existing numeric names, including after a backward clock adjustment.
Existing archives are not overwritten by a timestamp collision. Only numeric
`stream.jsonl.<number>` names participate in retention.

Retention is retried on startup and before reopening the current file. This
handles interruption after rename but before pruning or creating the next file.
A rename error leaves the old current file available for a later attempt. A
pruning error rejects the pending record and prevents subsequent appends until
pruning succeeds; it does not permit repeated rotations to grow disk usage
without bound. One extra archive can remain after the rename that encountered
the error. Records intentionally removed by configured retention are not counted
as queue drops.

The existing defaults remain a 128 MiB rotation threshold and three retained
archives. A single record is never split and may exceed a smaller configured
threshold. This is not a strict total-disk quota. Rotation still provides no
consumer cursor, gap notification or replay guarantee. Filesystem crash and
power-loss behavior has not been validated.

## Queue, health and shutdown

The existing 64-slot queue and conservative 16 MiB submission budget remain.
Queue overflow, oversized submissions, failed records and records rejected after
shutdown begins increment `dropped`. Byte accounting includes the active record
until it finishes; this is not a bound on total process memory.

Shutdown stops accepting records immediately and gives the worker up to ten
seconds to drain. It does not insert a sentinel into a potentially full queue.
If the drain deadline expires, queued records are discarded and counted, and a
shutdown error is logged. The current filesystem call cannot be cancelled by a
Python thread. The caller returns after its join budget while that daemon worker
may still finish its in-flight record. If it returns, the worker publishes final
counters and exits. The optional `Writer.close(timeout=...)` argument is for
controlled testing and internal callers; no CLI flag was added.

The health file keeps `pid`, `updated_at`, `writer_alive`, `written`, `dropped`,
`write_errors`, `last_error` and `queued_bytes`. Health-write failures are logged
and counted without killing capture. A later successful heartbeat includes the
error. When storage is blocked or health cannot be published, the CLI can only
see the last file and its age; immediate persistent diagnostics are not promised.
Error counts remain cumulative for the process, so successful recovery does not
erase an earlier error or make the existing doctor check healthy again.

## Evidence

Run from the checkout:

```sh
python3 -m unittest discover -s tests -v
```

`tests/test_capture_storage.py` adds 19 controlled tests using temporary files
and substituted file operations. They cover torn and large tails, a partial UTF-8
character, exact preservation of complete records, short reads and writes,
partial/zero-progress writes, failed rollback, ambiguous completion before an
exception, close errors, rotation rename/pruning failures, restart recovery,
timestamp collisions and clock reversal, health publication failure, queue
overflow and shutdown with a blocked writer. The existing runtime tests also
pass, including streaming-body handling and preservation of consumer offsets.

These are storage fault fixtures. They neither switch routing nor run a live
mitmproxy, and they do not simulate kernel failure or establish power-loss
durability. Record versioning, an independent reader contract, configurable
product-level retention, and end-to-end loss/replay reporting remain open.
