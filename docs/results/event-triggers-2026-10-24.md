# Event-driven triggers: measurements and pack recommendations (2026-10-24)

Evidence level: **dry run on a profile copy**, macOS arm64, system Python
3.9. This is not live integration or clean-machine acceptance. Nothing was
deployed, and no installed pack was changed.

## Setup

* Bench profile: APFS clones (`cp -c`) of the live profile's `packs/`,
  `resources/`, pack registry and current `stream.jsonl` (214 MB, cut at a
  record boundary). Three packs whose manifests use fields from the live
  checkout's uncommitted work (`tap.webmcp`, `x.webmcp`, `youtube.webmcp`) are
  disabled in the copy. All nine readers start at the tail.
* "Before" is `origin/main` at 66a168a. "After" is this branch.
* Controller: `service.main()` in-process, with the Hub and managed services
  stubbed out the same way on both sides. Real installed reader code. Idle
  means no new capture records.
* Background: the real 34 enabled packs and their schedules. Every command is
  replaced by one `/usr/bin/true` process, so CPU is host overhead only, not
  the commands' own work. "Before" starts the interpreter every 10 s like
  launchd's `StartInterval`. "After" is one resident host. Triggers come from
  the migration table below, injected in memory (packs not migrated). One
  warm-up pass runs before each 300 s window.
* Harness: `event-triggers-bench/` in the shared session notes folder (`setup_profile.py`,
  `run_service.py`, `run_background.py`, `bg_shim.py`, `run_drain.py`).

## Results

### Component controller, idle, 120 s

| | CPU of service.py | processes spawned/min |
|---|---|---|
| before (0.25 s loop × 9 readers) | 10.75 % of a core | 0 |
| after (kqueue growth wait) | **0.44 %** (0.53 % on an earlier commit) | 0 |

### Background scheduler, 300 s

| | CPU (host + stub children) | processes/min |
|---|---|---|
| before: launchd tick every 10 s | 4.80 % | 22.0 (12/min interpreters + 10/min commands) |
| after, no pack migrated | 2.06 % | 27.2 (1 resident host + 27/min commands) |
| after, triggers on idle sources | **0.96 %** | **8.4** |
| after, triggers on the real paths (this machine, in use) | 1.91 % | 12.4 |

Discovery (`background.tasks`) on this profile: 0.10–0.15 s CPU before and
0.03 s after the verify memo. With no pack migrated, commands now run at their
declared intervals. The 10 s tick stretched 10 s jobs to ~20 s and 30 s jobs to
~40 s, so "after" runs about twice as many commands for less than half the host
CPU. With triggers, the remaining runs are the jobs that stay on intervals
(antigravity hub refresh every 10 s, organizer every 30 s). On the real paths,
claude refresh ran 14 times in 5 min with `min_interval_seconds` 10, because
this session kept writing to `~/.claude/projects`. Choose its floor
deliberately: 10 s gives freshness during active use, while the current 120 s
gives the old cost bound.

An earlier "after" run measured 9.3 % CPU before the verify memo and
coalescing. That run found the cost: every pass re-hashed all 34 packs.

### Reader filter and batch: usage.meters replay over a 40 MB slice (921 records)

| | records/s | processes | reader CPU |
|---|---|---|---|
| old loop (origins, 50/run, 0.25 s pacing) | 3.8 | 357 | 225 s |
| new loop (origins + intended prefixes for claude.ai, api.anthropic.com, chatgpt.com; 500/run) | 5.5 | 251 | 158 s |

Each usage.meters invocation costs **~0.63 s CPU**. At startup it re-reads its
whole `usage.jsonl` (5 MB) and `activity.jsonl` (57 MB). That per-process
startup, not core scheduling, is most of the reader cost in the live audit (~2
spawns/s). Prefixes remove only 30 % of invocations here, because Kimi and
Cursor records match by substring and have no safe prefix (see below). The
remaining lever is on the pack side: index or tail its own files, or a
multi-record reader protocol.

Note: the drain ran real usage.meters code. Its default output
`~/tap-out/usage` is a symlink into the live profile, so the runs could write
live data. Checked afterwards: no rows were added to live `usage.jsonl` or
`activity.jsonl` in that window (the reader dedupes against newer live state),
and the passive-quota index only moves forward. The harness now gives reader
children a scratch `HOME`.

## Recommended migrations (not applied)

`P` is the profile root. These are plain declarations; whoever releases each
pack verifies them.

| Command (now) | Switch to | `trigger.watch` |
|---|---|---|
| claude refresh (120 s) | watch | `~/.claude/projects` |
| chatgpt work refresh (30 s) | watch | `~/.codex/state_5.sqlite`, `~/.codex/thread_history_1.sqlite`, `~/.codex/sessions`, `~/.codex/archived_sessions`, `data/readers/chatgpt.sessions` |
| chatgpt work review-pending (30 s) | watch | `state/packs/chatgpt.work/completion-reviews`, `data/packs/chatgpt.work/sessions`, `data/packs/chatgpt.work/reviews` |
| codex rollouts refresh (300 s) | watch | `~/.codex/sessions`, `~/.codex/archived_sessions` |
| cursor refresh (300 s) | watch | `~/.cursor/chats`, `~/.cursor/projects`, `~/Library/Application Support/Cursor/User/globalStorage` |
| kimi refresh (120 s) | watch | `~/Library/Application Support/kimi-desktop/daimon-share/daimon/runtime/kimi-code/home/sessions` |
| factory refresh (120 s) | watch | `~/.factory/sessions` |
| grok refresh (60 s) | watch | `~/Library/Application Support/Grok Bot/sand-client-persistence`, `~/tap-out/grok/native` |
| antigravity refresh (60 s) | watch | `~/.gemini/antigravity-cli` |
| gemini cli refresh (10 s) | watch | `~/.gemini/tmp` |
| usage native refresh (10 s) | watch | `~/tap-out/antigravity-hub/raw`, `~/tap-out/gemini-cli/raw` (outputs of hub refresh and gemini cli refresh, not profile data) |
| antigravity hub refresh (10 s) | keep interval | input is HTTP to Antigravity's DevTools port |
| gemini refresh (300 s) | keep interval | reads the capture stream itself; a watch would fire constantly |
| chatgpt organizer tick (30 s), chatgpt backfill (720 s) | keep interval | network-driven |

Suggested defaults: `debounce_ms` 500–2000 for chat transcript directories
(they append continuously while an agent runs), `min_interval_seconds` equal to
the current interval for the expensive ones (cursor, codex), and a backstop of
3600 s.

Reader `input.path_prefixes` that are provably safe today:

| Reader | Prefixes | Also |
|---|---|---|
| chatgpt.sessions | all three origins: `/backend-api/conversation` | |
| chatgpt.media | chatgpt.com: `/backend-api/estuary/content`, `/backend-api/conversation/` | |
| gemini.sessions | gemini.google.com: `/_/` (code matches a substring `/_/BardChatUi/data/batchexecute`) | |
| grok.sessions | api2.cursor.sh: `/aiserver.v1.GrokBotService/` | drop `https://grok.com` from origins; never acted on |
| linkedin.archive | www.linkedin.com: `/voyager/api` | |
| x.posts | x.com: `/i/api/graphql/` | drop `https://video.twimg.com`; never acted on |
| youtube.subtitles | both origins: `/youtubei/v1/player`, `/api/timedtext` | |
| x.subtitles | none until the pack stops inserting a `deliveries` row for every record; then x.com `/i/api/graphql/`, video.twimg.com `/subtitles/` | |
| usage.meters | none is strictly safe: `claude.py` checks `"individual_plan_pricing" in url` and copilot/kimi use substring checks with no host check. Intended: claude.ai `/api/organizations/`, `/v1/code/sessions/`; api.anthropic.com `/v1/messages`, `/v1/code/sessions/`; chatgpt.com `/backend-api/`. Anchor the pack's matchers first. | |

Several readers exit 1 on an unknown `record_version` before any URL check, so
filtering in core also keeps unrelated records from failing them.

## Migration required to take effect

* After deploying this Core, run `tap on` or any pack enable/disable so
  `reconcile` rewrites the background plist to the resident form. A profile
  whose plist still has `StartInterval` keeps working in tick mode, but
  triggers then act only through their backstop interval.
* Packs that adopt `trigger` or `input` require this Core, because older
  validators reject the fields.
