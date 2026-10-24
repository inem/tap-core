# Event-driven triggers

Core wakes work on change instead of on a short timer, in three places. Each
is opt-in by declaration, or else keeps the previous behavior.

## Command watch trigger

A scheduled command may add `trigger` next to `schedule`:

```json
{
  "path": ["claude", "refresh"],
  "schedule": {"timeout_seconds": 300},
  "trigger": {"watch": ["~/.claude/projects"], "debounce_ms": 500, "min_interval_seconds": 10}
}
```

| Field | Meaning |
|---|---|
| `trigger.watch` | 1–16 paths. `~/…` (the user's home), absolute, or relative to the profile root (`data/readers/chatgpt.sessions`). No `..`. Directories are watched recursively. A path that does not exist yet is watched through its nearest existing ancestor; only its own creation and changes below it count. |
| `trigger.debounce_ms` | 0–60000, default 500. A burst of changes runs the command once after this much quiet. A burst that never quiets still runs after 30 s. |
| `trigger.min_interval_seconds` | 1–86400, default 10. Floor between the end of one run and a triggered start. A command that writes inside its own watch paths therefore re-runs at most this often. |
| `schedule.interval_seconds` | Optional with a trigger. It becomes a backstop, default 3600 (1 h). Without a trigger it stays required, 10–86400. |

`trigger` requires `schedule` (for `timeout_seconds` and `background.run`).
Commands without a trigger are unchanged: same schedule and fingerprint, so an
upgrade does not re-run them. Core versions before this one reject `trigger`
and a schedule without `interval_seconds`. A pack that adopts these fields
requires this Core.

### Host

The background LaunchAgent is now resident (`KeepAlive`, `--resident`). Before,
launchd started an interpreter every 10 s. The resident host:

* watches every trigger's paths with macOS FSEvents and marks the task pending;
* runs pending tasks after debounce and the floor, through the same `run_once`
  pass as interval jobs (`wake=`). Leases, fingerprints, timeouts and logs are
  unchanged;
* otherwise sleeps until the earliest interval deadline (jobs due within 2 s of
  each other share one pass), the usage-freshness wake, or 30 s at most. The
  30 s bound is how quickly it notices pack enable, disable or update;
* exits when Core's own `tap_core/*.py` files change, so launchd restarts it on
  new code (throttled to 10 s);
* memoizes pack integrity hashes by file identity (inode, size, mtime, ctime).
  Any content change re-hashes, so drift is still caught.

Capture `off` still does not stop background work. If FSEvents cannot start, the
task polls every 60 s instead. `state/background-host.json` records each watch
as `watching` or `polling` with its error. `tap status` shows it under
`background.host`, and `tap doctor` names polling tasks. The file is the host's
last observation, not proof that it is alive.

## Readers are woken by capture, only for their own records

Capture routes notifications instead of every reader watching one shared
stream.

1. The component controller publishes `state/routes/components.json`. It
   lists one target per reader, `reader:<name>`, with the reader's granted
   origins and its `input.path_prefixes`, plus the path of a Unix datagram
   socket the controller listens on.
2. After the capture writer appends a record to `stream.jsonl`, it matches the
   record against every published route. It sends each owner whose targets
   match one datagram `{"v": 1, "targets": [...]}`. Matching is
   `routes.record_matches`, the same function a reader's own filter uses.
3. Each reader waits on its own wake event, which is cleared at the start of
   every pass. An irrelevant record never wakes it. A relevant one starts a
   pass at once; there is no idle floor.

A notification is only a hint. Sending it never blocks or fails capture, and a
reader always reads from its own cursor:

- **Missed notification** (controller restarting, socket full): delays work
  until the next notification for that reader or the backstop. No record is
  lost.
- **Startup and every reader attach:** catch up from the cursor first. Routes
  are published before a reader starts. One extra pass 2.5 s after attaching
  covers capture's 1 s route reload.
- **Backstop:** 180 s.
- **No bindable socket:** the controller falls back to the stream-growth wait
  (kqueue) with its 1.5 s floor, and logs it.

The socket path is `state/wake/<owner>.sock`. When that is longer than
`sun_path` allows, a private per-profile directory under the system temp
directory is used; the routes file records whichever applies.

A scan is cheap. The journal identifies a segment by its first record, and
caches that per file `(dev, inode)`. It revalidates the cache with two small
reads, not by re-reading a first record that can be megabytes.

## Command trigger: capture

A scheduled command can also be started by captured traffic:

```json
"trigger": {"capture": {"origins": ["https://gemini.google.com"],
                        "path_prefixes": {"https://gemini.google.com": ["/_/BardChatUi/"]}},
            "debounce_ms": 3000, "min_interval_seconds": 60}
```

- `capture.origins`: 1–64 exact origins, each granted in `access.origins`.
- `capture.path_prefixes`: optional, keyed by one of those origins.
- A trigger needs `watch`, `capture` or both.
- The resident background host publishes `state/routes/background.json` (targets
  `command:<task key>`) and listens on its own socket.
- A matching record marks the task pending, exactly like a file change. Debounce,
  `min_interval_seconds` and the backstop interval apply.
- Without a socket, capture-triggered tasks poll every 60 s. `status` lists
  them under `background.host.captures`, and `doctor` names them.

## Reader input: path prefixes and batch

A reader may narrow its input within its granted origins:

```json
"reader": {
  "file": "reader.py", "interface": "python-jsonl-v1",
  "input": {"path_prefixes": {"https://chatgpt.com": ["/backend-api/conversation"]}}
}
```

Keys must be exact origins granted in `access.origins`. Each origin takes 1–64
prefixes, each starting with `/`, with no `?` or `#`. Matching is a plain
`startswith` on the URL path (query excluded). An origin not listed keeps all
of its paths. Core filters before the reader runs: a filtered record spawns no
process. It still counts as `processed`.

Filtered records advance the checkpoint in memory. They are persisted with the
next checkpoint write (before an invocation, or at the end of the run), not by
one fsync'd write each. A crash loses at most the filtered prefix, which is
filtered again. The replay batch rose from 50 to 500 records per run. The fresh
lane's recovery pass reads up to 500 records but still makes at most one
invocation per pass. Each admitted record is still one child process
(`python-jsonl-v1`). Batching records per process is a protocol change and is
not part of this change.

Stage-2 measurements are in [results/reactive-routing-2026-10-24.md](results/reactive-routing-2026-10-24.md). Measurements and the per-pack recommendations for stage 1 are in
[results/event-triggers-2026-10-24.md](results/event-triggers-2026-10-24.md).
