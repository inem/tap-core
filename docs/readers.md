# Independent readers over retained capture

This first #9 slice adds explicit finite reader runs to the existing profile CLI.
It uses record v1 / the journal scanner from #27. It does not install a pack,
start a daemon, select app-specific data, or change capture/network lifecycle.

## Run two readers

Use an existing development profile from [runtime setup](runtime.md). A reader
can process retained files while capture is stopped; its command does not start
the backend or change system routing. This example uses the new idempotent
SQLite projection fixture, not personal traffic:

```sh
python3 tools/check_readers.py
```

That command creates a temporary synthetic profile, writes A → B → A through the
real Writer, invokes two real reader CLI processes with different batch sizes,
resumes and replays one, and verifies that the other checkpoint stays unchanged.
Replay changes its projection behavior, and retrying a processed prefix verifies
that a receipt prevents the latest result from rolling backward.
The profile and all fixture results are removed on completion.

For your own existing profile, create an explicit definition file:

```json
{
  "version": 1,
  "revision": "reader-1",
  "command": ["/absolute/path/to/python3", "/absolute/path/to/reader.py"],
  "config": {"prefix": "example"}
}
```

The command is argv, not shell text; its executable must be absolute. Relative
arguments resolve from the profile directory. `revision` is the author's version
of the reader behavior. Bump it when changing code or dependencies: the runner
cannot detect arbitrary edits to imported files. Version, revision, argv and
config together bind an existing checkpoint. A changed definition requires a
new reader name or explicit replay; it never silently inherits old progress.
Config must contain JSON values with finite numbers at every nesting level.
`NaN`, `Infinity`, `-Infinity` and exponents that overflow to infinity are rejected
before progress is created or changed, including through Python `run`/`replay`.

```sh
./tap --profile "$PWD/.tap-dev" reader run slow --definition reader.json --max-records 1
./tap --profile "$PWD/.tap-dev" reader run fast --definition reader.json
./tap --profile "$PWD/.tap-dev" reader run slow --definition reader.json
./tap --profile "$PWD/.tap-dev" reader status slow
./tap --profile "$PWD/.tap-dev" reader replay slow --definition reader.json
```

`run` processes at most 100 complete records by default, then exits. `--timeout`
is the per-record child deadline (default 30 seconds; positive, at most 300).
`replay` resets only that reader's checkpoint to available history, increments its
generation and returns; the next `run` performs the work. It retains output and
reader-owned state. Clearing/rebuilding a projection is the reader's own policy.
No archived material is restored. A new reader starts at available history and
does not reset any existing reader.

For a pack code update that preserves the same input and output meaning, `rebind`
is the explicit alternative to replay. Stop the profile, install the new pack,
construct its exact new reader definition (including the installed code path),
then run `tap reader rebind NAME --definition NEW.json --expect-hash OLD_HASH`.
It preserves the cursor and processed count, increments the generation and
writes a receipt with the previous checkpoint. If this reader also has a fresh
checkpoint, rebind it with `--lane fresh` and its own expected hash before
restarting; otherwise the fresh lane refuses a changed definition. An unexpected old hash refuses
without changing progress. Enable the new pack version before starting the
profile again. If interrupted between rebind and enable, complete the enable
while stopped or rebind to the previous definition using the new checkpoint
hash. The operator and pack author must verify compatibility; Core cannot infer
it from a version number.

## Input, output and progress

For each record, the host starts one child, provides one UTF-8 JSONL record on
stdin followed by EOF, drains stdout/stderr and waits for exit. Exit 0 means the
reader asserts successful processing; nonzero, timeout and excess output fail.
Stdout itself is not an acknowledgement. This preserves the existing
`python-jsonl-v1` input shape while choosing a deliberately simple per-record
process boundary. It has startup cost; long-lived workers and throughput tuning
remain follow-ups, not implied guarantees.

The child receives `TAP_PACK_CONTEXT` with `reader_id`, `reader_generation`,
`config`, `state_dir`, `output_dir`, and `log_dir`, allowing the existing fixture
reader to run. These
are invocation context fields, not a claim of installed pack API support, grants
or sandboxing. The host owns `state/readers/<name>/checkpoint.json`; child state
is separate under `state/readers/<name>/work`, output under `data/readers/<name>`
and logs under `logs/readers/<name>`. Owned directories are mode 0700; checkpoints
and captured logs are replaced through private temporary files.

`TAP_READER_DELIVERY_ID` identifies a captured journal entry and remains stable
across retry and explicit replay in the same profile. It is not a body hash:
A → B → A contains three different delivery IDs. `reader_generation` is a
positive integer that increments on explicit replay. The opaque
`TAP_READER_INVOCATION_ID` combines reader name, generation and delivery ID: it is
stable across retries in a generation and changes on replay, even when the
definition is unchanged. IDs are local to a profile; do not treat them as global
external-action keys without your own namespace and reconciliation policy.

Use invocation IDs for projection receipts that must permit recomputation on
replay. The SQLite fixture commits a generation-scoped receipt and projection
update in one transaction. A new generation updates the existing row for each
retained delivery using the current reader behavior. Repeated invocations in
that generation skip both the projection and latest-result update, so retrying
an older processed prefix cannot roll latest backward. During a partial replay,
output can contain both old and recomputed rows; rows no longer retained are not
removed. Atomic publication of a complete rebuilt projection remains reader
policy. The original append-only pack fixture also runs but may append duplicate
results after uncertain completion.

Readers deduplicating external effects across replays may instead keep using the
delivery ID (with their own action scope). An explicit projection replay is not
authorization to repeat an external action. Choose the receipt identity to match
the operation; exactly-once external effects are not supplied by these IDs.

### Compatibility with the first reader fixture

Existing version-1 checkpoints and cursor-based delivery IDs remain compatible;
generation context and invocation IDs are additive. The persisted `inflight`
field now records the invocation ID. A checkpoint saved by the earlier runner
may still contain a delivery ID there until its next invocation; the host resumes
from its acknowledged cursor and generation in either case.

Existing readers must adopt generation-scoped receipts to recompute on replay.
The old SQLite fixture used delivery rows as unscoped receipts and cannot safely
infer their generation. The updated fixture fails clearly when it finds that
schema. Use a new reader name to build a fresh projection, or deliberately move
aside its derived `projection.sqlite3` and explicitly replay before running.
Keep any old database/WAL companions together when archiving the stopped reader.
The runner never deletes or migrates reader-owned state automatically. For a
non-fixture reader, follow that reader's migration and external-effect policy.

The runner persists an in-flight invocation ID before invoking the child and
advances the cursor only after successful completion and checkpoint replacement. Failure to
save that replacement preserves the old cursor even in the error handler.
Processing stops at the failed record; a later explicit `run` retries it. A
crash after output but before checkpoint can repeat input. Readers with external
side effects must supply their own idempotency/reconciliation; this is not an
appropriate blind-retry wrapper for a fork/create action. Exactly-once external
effects are not promised.

## Isolation and failures

Each reader has its own lock, checkpoint and generation. The host does not hold
the profile mutation lock while processing records, so different readers can run
independently of each other and of network commands. Concurrent runs/replay for
the same name fail before processing with an error naming the busy reader. Other
readers and profile commands keep their independent locks. The lock descriptor
is inherited by the child (`TAP_READER_LOCK_FD`) so a controller crash does not immediately allow an
overlapping invocation. A trusted reader must keep it open until completion.
Children must remain finite workers and must not daemonize or escape their
invocation's process group.

During normal operation a timeout/output failure kills only the invocation's
process group and reaps its leader. A killed controller cannot enforce its own
deadline; an orphan child can keep the lock until it exits or is separately
stopped. The next controller cannot claim that old work was safely cancelled.
Managed scheduling and orphan recovery are implemented by #32/#43; see
[managed components](managed-components.md) for the profile lifecycle. The controller-crash fixture verifies that progress stays put and
the inherited lock blocks overlap until the known fixture child is stopped.

Combined captured stdout/stderr is capped at 1 MiB per invocation. The last
invocation's bounded output is stored in `last-stdout.log`/`last-stderr.log`; logs
may contain payloads emitted by a reader and are not included in CLI status.
Trusted reader code may write other files or use network; these bounds are not
an execution sandbox or a total disk/memory quota.

`reader status` shows the last persisted phase (`ready`, `running`, `idle`,
`failed`, `gap`), counters, generation and in-flight ID. This is historical
state, not an assertion that a PID is alive. A stale `running` phase after crash
is intentionally visible. Missing state is unconfigured; malformed/unreadable
state is an error and is never reset automatically. Checkpoint timestamps must
be finite JSON numbers; NaN, Infinity and exponent overflow fail before status
serialization or run/replay can change progress. This is a bounded addition,
not completion of all diagnostics in #13.

A missing journal anchor is recorded in `state/readers/<name>/last-gap.json` and
the reader continues from the earliest retained record without changing its
generation or clearing its projection. This accepts the already-unavoidable loss
of the unavailable prefix so later capture can keep flowing. Because the missing
cursor cannot order itself against surviving segments, some retained records may
be delivered again with their original stable delivery IDs; readers still own
effect deduplication. If retained history is itself ambiguous, the fresh scan
still stops with `gap`. A malformed complete
record stops before later records; an unfinished active tail waits for a later run. See
[capture records](capture-records.md) for conservative retention-gap behavior and
legacy compatibility limits. No fsync/power-loss durability was added.

## Evidence and remaining scope

Run the existing suite and the synthetic CLI checks:

```sh
python3 -m unittest discover -s tests -v
python3 tools/check_readers.py
python3 tools/check_reader_parity.py --output docs/results/reader-delivery-parity-YYYY-MM-DD.json
python3 tools/check_pack_fixtures.py --bun /absolute/path/to/bun
```

`check_reader_parity.py` runs the same fixed A → B → A corpus through an
independent one-record subprocess baseline over stable source JSONL (without
`Writer`, `Journal.scan` or `Reader.execute` in the direct path) and through
`reader run`, compares the SQLite projection (order + latest), lists intentional
differences, and records one-process-per-record wall time plus commit, reader /
corpus digests and measurement environment. It does not claim batch-stdin pack
identity or a throughput SLA. See
[reader-delivery-parity-2026-09-07.json](results/reader-delivery-parity-2026-09-07.json).

The fixtures cover separate progress, restart, new-reader history, child failure
before/after effects, checkpoint failure, timeout, output overflow, controller
crash, locks, rotation/gaps, changed-behavior replay, receipt migration failure,
partial-replay retries, finite config validation, malformed input, torn tails
and A → B → A.
They use real subprocesses and temporary data. No live network, certificates,
launchd or production TAP state is involved. Current CI runs Python tests and
pack page fixtures; the full Bun protocol suite is also run locally.

Local verification on 2026-09-07 passed all 147 tests (24 reader tests), both
pack fixtures and the [synthetic CLI check](readers-fixture-2026-09-07.json).

Managed scheduling/orphan recovery (#32/#43) and installed reader/handler
bindings (#57) are in main. The installed end-to-end example remains #14;
aggregate diagnostics and wider failure/upgrade acceptance remain #13/#7.
Delivery parity and invocation-cost evidence for the declared n=3 corpus are in
the parity harness above; a larger corpus or long-lived workers are a follow-up
threshold, not implied by this measurement.

The negative parity test corrupts only runner input and requires failure. Direct
also runs with Journal.scan disabled. Repeated-delivery effects are covered by
`test_failure_after_effect_is_deduplicated_by_reader_receipt`,
`test_a_b_a_updates_latest_instead_of_deduplicating_by_content`, and
`test_partial_replay_retry_does_not_roll_latest_back` in the reader suite.
The three distinct A/B/A records in the cost harness are not a retry scenario.
