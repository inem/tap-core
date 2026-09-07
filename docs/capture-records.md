# HTTP capture records and verifiable journal positions

This #8 slice extends the existing writer from PR #22. It does not replace JSONL,
change profile paths or introduce a reader service. The generic proxy emits HTTP
response observations; application catalogs, message formats and actions remain
pack responsibilities.

## Record version 1

One UTF-8 JSON object plus a newline is one complete record. The existing
`data/stream.jsonl` and numeric archives can contain both old and v1 records.
Unknown fields within v1 may be ignored; an unknown version must be rejected.
`tap_core.records.decode_record` validates the version and required types and
rejects malformed JSON, duplicate fields and invalid UTF-8. The executable
examples are `fixtures/capture/v1.jsonl`.

| Field | Meaning |
| --- | --- |
| `record_version` | Integer `1`; independent of pack API and profile versions. |
| `record_id` | UUID identifying this capture observation. Stable when saved, unique for repeated identical responses; not an HTTP request ID or an exactly-once key. |
| `ts` | Nonnegative Unix capture time in seconds. Does not establish ordering across clock changes. |
| `method`, `url`, `status`, `ctype`, `ua` | Existing request method/URL, response status/content type and observed User-Agent. User-Agent does not establish source application identity. |
| `size` | Existing nonnegative size hint: Content-Length if usable, otherwise buffered raw length when available, otherwise zero. Not a measured streamed-body byte count. |
| `streamed` | Whether the backend streamed this response, including backend size policy. |
| `body_kept`, `body_reason` | Boolean plus `retained`, `media_type`, `streamed`, `unavailable`, `oversize`, `unbounded` or `oversize_decoded`. |
| `body` | Text only when retained, including an empty string for a captured empty body. Omitted otherwise. Decoding uses the existing backend `get_text(strict=False)` behavior, not byte-exact storage. Bodies over `max_body_bytes` are omitted before or after decode (`oversize` / `oversize_decoded`). Missing Content-Length alone does not force omission; large unknown lengths rely on backend `stream_large_bodies`. Serialized records that would exceed the journal reader limit omit bodies before append. |
| `req_body_kept`, `req_body_reason` | Boolean plus `retained`, `streamed`, `unavailable`, `response_not_retained` or `oversize_decoded`. Request bodies are considered only when the response body is retained, preserving the existing selective-capture policy. |
| `req_body` | Text only when retained; an omitted/streamed request is no longer represented by an empty-string placeholder. |

The current media policy streams binary and SSE without reading their bodies.
Those records use `media_type`. A body-eligible response that the backend streams
uses `streamed`; this intentionally does not claim to know whether size policy or
another addon caused it. A nonstreamed body unavailable from the backend uses
`unavailable`. A retained empty body is distinct from an unavailable body.

No WS frame capture is introduced. An HTTP upgrade response is only an HTTP
observation. Missing/failed exchanges without a response are not represented by
this hook. Queue drops and write errors remain process health counters, not
invented records; v1 does not prove that every network exchange was captured.

## Existing material and migration

Unversioned records (or explicit version 0) are accepted by the compatibility
reader with minimal URL/status and optional body/flag type validation. No old
file is rewritten. Missing historical metadata stays missing; the reader does
not invent a UUID, omission reason or coverage guarantee. Use
`allow_legacy=False` when only fully validated v1 is permitted.

The existing example reader accepts old and v1 records and rejects other
versions. Its stdin interface stays JSONL. A future host validates the full
record before delivery; the example's version check is not a replacement for
that validation. The compatibility test sends a real recovered/rotated mixed
journal to the existing Python reader and checks its output.

## Internal storage input for independent readers (#9)

`Journal(data).scan(after=None)` yields `Entry(record, cursor)` for the retained
snapshot. `after=None` explicitly starts from available history, without claiming
that older material never existed. Pass the last successfully consumed opaque
cursor to resume. The scanner neither persists it nor changes another reader's
progress. There is no automatic replay, polling, retry or acknowledgement.

```python
from contextlib import closing
from tap_core.journal import Journal

with closing(Journal(profile_data).scan(after=saved_cursor)) as entries:
    for entry in entries:
        consume(entry.record)
        save_own_cursor(entry.cursor)  # only after successful consumption
```

`consume` and `save_own_cursor` are caller responsibilities in this example;
transactional output/checkpoint coupling, crash recovery and replay control are
explicitly #9 work. A crash between output and checkpoint can repeat an effect.

The current cursor encodes a format version, capture-directory fingerprint,
segment identity, line ordinal and a hash of the acknowledged line. Consumers
must store it opaquely instead of generating offsets or parsing filenames.
Moving a profile or changing journal bytes can invalidate its cursors. Unknown
cursor versions are errors, not invitations to restart automatically. Cursors
contain no URL/body/credentials, but are not authentication or authorization.

A segment is identified by the hash of its first complete line. That line is
stable during append, rename and restart, and includes a unique observation ID
for v1. Identical first lines in separate legacy segments are ambiguous: the
scanner rejects them rather than guessing. This is a conservative compatibility
limit, not a new segment header or a rewrite of old data.

## Rotation, deletion and errors

The scanner opens a bounded set of descriptors, verifies that names still point
to those files and reads only the sizes observed when opening them. A rotation
race returns `JournalChanged`; the caller can retry with the same saved cursor.
Descriptors keep selected records readable if retention unlinks the file during
a scan. Newly appended records belong to the next scan. Close the generator on
early exit to release descriptors and space promptly.

A missing acknowledged segment, changed anchor, or truncated position raises
`JournalGap` before any newer records are yielded. The caller must surface the
uncertainty and choose replay/reset explicitly. Even a fully consumed segment
removed before its next scan causes a conservative gap: there is no successor
ledger proving whether records were missed. No exact lost-record count is
claimed. A missing/unreadable directory is an error, not empty history.

Only complete lines are yielded. An unfinished active tail is deferred without
advancing a cursor; writer recovery may later remove it. A malformed complete
record, unsupported version or unfinished archived line fails explicitly.
Failures after some records were yielded do not undo those records: the caller
retains its last successful cursor. No cursor moves past the failed record.

This covers the existing writer's oldest-first retention. Arbitrary external
archive deletion/reordering or rewriting old records is not a supported storage
operation; this scanner is not an integrity ledger for all earlier history.
It does not synchronize multiple writers, add fsync/power-loss durability,
restore deleted files or provide an exactly-once delivery guarantee.

## Bounds and remaining work

A scan opens at most 64 segments and allows at most 32 MiB per JSONL line,
including its newline. A smaller reader allocation limit can be selected via
`max_record_bytes`; this is not a whole-process memory quota. The current writer's
conservative 16 MiB queue budget bounds normal emitted records below that reader
limit. Oversized legacy records fail explicitly. Listing and line reads are
bounded; processing costs scale with retained segments and the acknowledged
position within its segment. There is no seek index or performance claim yet.
The scanner runs outside capture hooks and should not be busy-polled.

Writer limits, archive naming and error counters are profile-configurable via
`profile.capture` / `TAP_CORE_CAPTURE` (#8). Keys: `stream_large_bodies` (backend
cutoff, default 4 MiB), `segment_bytes`, `keep_rolls`, `queue_slots`,
`queue_bytes` and `max_body_bytes` (retained/decoded text budget). Defaults match
the previous hardcoded plist/writer. Bodies over `max_body_bytes` are omitted
before decode with `body_reason=oversize` / `unbounded` / `oversize_decoded`;
backend streaming above `stream_large_bodies` stays `streamed`. Queue drops are
writer health counters, not JournalGap. Long-running independent readers,
delivery parity and end-to-end recovery remain #9/#13 work.

## Verification

Run `python3 -m unittest discover -s tests -v` and
`python3 tools/check_pack_fixtures.py --bun /absolute/path/to/bun`.
The new tests exercise version compatibility, actual Capture records, old/new
reader output, real Writer rotation and restart, deleted checkpoints, torn tails,
malformed records, ambiguous legacy segments and controlled listing races.
The opt-in `tools/check_runtime.py` additionally validates emitted v1 records,
body reasons and unique IDs through the actual backend on two temporary explicit
profiles. It does not mutate system routing or establish HTTPS/WS acceptance.

Recorded on 2026-09-07: all 122 tests passed (26 new record/journal tests),
both pack fixtures passed, and the two-profile live backend check passed.
[Live result](record-contract-live-2026-09-07.json) records v1 validation, body
reasons, unique IDs, KeepAlive, independent off/on and unchanged system settings.
All temporary jobs were removed. No production capture or credentials were used.
