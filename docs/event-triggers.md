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

## Readers wake on stream growth

Component-controller readers no longer loop every 0.25 s. They block on a kqueue
watch of `data/stream.jsonl` (write/extend/rename/delete) and its directory
(rotation creates a new file), with a 30 s backstop. While records are pending,
runs repeat without sleeping. Scans start at most every 0.25 s, the former
cadence, so a busy stream costs no more than before. Without kqueue the former
0.25 s loop is used, and the controller log says so.

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

## Connecting an existing operation (e.g. a launchd `WatchPaths` binding)

The contract, on these branches only (not in an installed Core yet):

| Step | What Core provides |
|---|---|
| Declaration | An installed **pack command** with `schedule.timeout_seconds` and `trigger`. `trigger.watch` takes 1–16 paths, each watched recursively. #276 adds `trigger.capture`. |
| Relevant event | FSEvents on the watched paths, with debounce and `min_interval_seconds`. With #276, also captured records matching `trigger.capture`. |
| Operation | The command runs as `process-argv-v1` under the pack's execution lease, with the normal command context. |
| Result / error | Each run writes `state/background-runs/<run>/started.json` and `finished.json` (`tap.background-run/v1`: trigger kind, exit code, phase). It has a per-command log under `logs/background/`. Each watch's state (`watching` / `polling` + error) is in `state/background-host.json`, `tap status` and `tap doctor`. |

Not covered, so these are holes rather than hidden behaviour:

1. **Only pack commands.** A standalone script must be packaged as a command pack to use this.
2. **Static watch sets.** `trigger.watch` is fixed per pack version. An operation whose sources come from its own catalog can't update the list without a new pack version, and nothing records when catalog and declaration diverge.
3. **Own outputs.** Only the host's own state and log writes are ignored. A command whose outputs land inside its watched paths re-triggers itself, bounded by `min_interval_seconds`.

Replacing an interim native binding (such as launchd `WatchPaths` with `RunAtLoad` and no resident process) is warranted only when all of these hold:

- the installed Core accepts `trigger` and runs the resident host (`tap status` → `background.host.mode == "resident"`);
- the operation is a pack command;
- its whole watch set fits a static declaration, or Core supports derived watch sets;
- its outputs are outside the watched paths.

Until then the native binding is the honest choice, provided its drift from the derived watch set is checked.

Measurements and the per-pack recommendations are in
[results/event-triggers-2026-10-24.md](results/event-triggers-2026-10-24.md).
