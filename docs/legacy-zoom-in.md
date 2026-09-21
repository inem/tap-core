# Legacy TAP zoom-in: inventory, parity matrix and compatibility-pack plan

Tracking: [#197](https://github.com/inem/tap-core/issues/197) (zoom-in) and
[#194](https://github.com/inem/tap-core/issues/194) (parity matrix). Inspected
2026-09-21 against legacy `tap` HEAD `54ccb21` (private repo) and tap-core
`origin/main` `9c226ec`. Everything here was read from source and from the
installed machine; no legacy lifecycle command was executed.

Legacy TAP is **source material, not a service to keep running**. Core owns the
proxy, capture, bridge, lifecycle, doctor/status, pack integrity and routing.
A legacy behaviour is carried over only when it is still valuable *and* can run
under Core contracts.

This document builds on, and does not repeat,
[legacy-cli-lifecycle.md](legacy-cli-lifecycle.md),
[legacy-cli-fixtures.md](legacy-cli-fixtures.md),
[extraction-start.md](extraction-start.md) and [live-slice.md](live-slice.md).
Those cover `install/on/off/status/doctor/where`, `capture.py`, the ChatGPT
reader and `site-probe`. Everything else below was uncharacterized until now.

## How to read the matrix

**Status** uses the #194 vocabulary:
`ported` (same behaviour, Core/pack owner) · `replaced` (different mechanism,
same user outcome) · `missing` (valuable, no owner yet) · `retired` (explicitly
not carried over) · `out-of-core` (lives outside Core and packs by design).

**Target**: `native` (Core) · `pack` (ordinary pack) · `compat` (the
`legacy.tap` compatibility pack family, see below) · `drop` · `unknown`.

**Evidence** cites what shows the behaviour was really used: shell history of
the operator, agent sessions in the session archive, or state on disk.

## 1. Where legacy TAP lives

| What | Location | State on 2026-09-21 |
|---|---|---|
| Source | `~/Code/tap` (private GitHub repo), CLI on PATH as `tap-legacy` | Present. `on`, `reload`, `install`, `off` refuse unless `TAP_LEGACY_FORCE=1` |
| Recorder job | launchd `com.tap` → `mitmdump -s capture.py [-s mutators/_loader.py] -p 8899` | `launchctl disable`d, plist kept as `com.tap.plist.disabled`, port 8899 closed |
| Reader job | launchd `com.tap.read` → `elixir read/run.exs --loop` | disabled, plist kept as `.disabled` |
| Organizer job | launchd `com.tap.chatgpt-organizer` (written by `tap chatgpt organize on`) | absent; superseded by pack `chatgpt.organizer` |
| Session relay job | launchd `com.tap.claude-disk-source` → script living inside the output archive | **still running** — see §6 |
| Probe hub | `probe/tap-probe` (Bun hub + 2 sources, loopback 47991, pid files, not under launchd) | stopped |
| Raw capture | `~/.tap/stream.jsonl` + 3 archives `stream.jsonl.<ts>` (~7 GB each, ≈20 GB total, mode 0644) | frozen at retirement |
| Reader cursor | `~/.tap/read.offset` (one shared byte offset) | `0` |
| Harvested credentials | `~/.tap/auth/<host>.{cookie,authorization,user-agent}` (0600) | **last refreshed at retirement; nothing refreshes them now** |
| Blob store | `~/.tap/blobs/<sha256>.<ext>` + `manifest.jsonl` | 188 files |
| Probe state | `~/.tap/probe/` (token, allowlist, `bus.jsonl` journal, `trace.jsonl`) | frozen |
| Integration state | `~/.tap/dopo-transcripts`, `~/.tap/dopo-chatgpt-links`, `~/.tap/chatgpt-backfill`, `reconcile`, `occurrences`, `session-fold-watcher`, `historical-session-folds` | frozen |
| Reader output | `~/tap-out/<service>/` | mixed: some dirs now fed by Core packs, some legacy-only — see §5 |
| CA | `~/.mitmproxy/` | **shared with Core — must not be removed with legacy** |
| Proxy rights | `/etc/sudoers.d/tap` (scoped `networksetup` proxy verbs) | present; Core `finish-setup` installs its own scoped rule |
| Logs | `/tmp/tap.log` (unrotated, world-readable, contains URLs with tokens), `/tmp/tap-read.log`, `/tmp/tap-preflight.log`, `/tmp/tap-probe-*` | stale |
| Config | `directions.json` (phases + bypass table), `mutators/.active` (opt-in marker), `schemas/*.json` | in repo |

Who actually used what: the operator's own shell history contains only
`reload` (34), `status` (11), `on` (11), `where` (5), `doctor` (5), `off` (4),
`install` (1). Mutators, readers, `retro/`, `directions.json` and the ChatGPT
commands were driven by coding agents (session archive, 2026-08-25 … 09-17).

## 2. Lifecycle, proxy ownership, diagnostics

| Legacy capability | Core equivalent | Status | Still needed? · evidence | Target | Ownership conflict | Required Core API / contract | Acceptance test | Risk |
|---|---|---|---|---|---|---|---|---|
| `on`: start → arm all services → prove traffic → else disarm | `tap on` (start → arm → probe; snapshot in `state/proxy-before.json`) | ported | Yes · operator history (11×) | native | Legacy arms port-agnostically and overwrites any prior proxy | — (#177 done; verification policy in #189) | #192 live lifecycle acceptance | Low |
| `off`: disarm verified before stopping | `tap off` + drain window (#176) | ported | Yes · history (4×) | native | Legacy `off` disarms **whoever** owns the proxy, including Core | — | #102 regression stays green | Low |
| `reload`: preflight on a throwaway port, swap, disarm on failure; also restarts Probe + sources | none as one verb. Page-only pack changes are hot; reader/handler binding changes need `off`→`on` | missing | **Yes — most-used legacy verb (34×)** | native | Legacy couples runtime reload with app restarts | One-command recovery/reload that never drops routing: #119 | reload with a broken addon leaves routing intact and exits non-zero | Medium: without it operators fall back to `off`/`on` |
| `status` (proc/port/armed, squatter, fd pressure, armed N/M) | `tap status` v4 | replaced | Yes | native | Legacy `armed()` is port-agnostic: legacy `status` reports "capturing" while Core owns the proxy | Truthfulness under partial knowledge: #191 complete | status names active-only rescue separately from full system policy | Low |
| `doctor` (backend, CA file, cert present, launchd, sudo rights, port, outward probe, fd headroom, bypass drift) | `tap doctor` | replaced | Yes · history (5×) | native | none | HTTPS decryption/default curl trust #193 complete; fd-headroom meter and declared-vs-applied bypass drift remain follow-ups | profile-CA issuer and default curl trust probes pass without `-k` | Low |
| `where` (every file, sizes, recency, per-service harvest) | `tap where` | replaced (narrower) | Yes · history (5×) | native | none | #96 / #100; pack-contributed `where` providers | `where` lists every reader output dir with newest-file age | Low |
| `directions` (hidden): phases, bypass table, system/`NO_PROXY` drift, phase drift | none | missing | Partly: drift report yes, "phases" no (nothing routes on them) | native | none | part of #6 | declared passthrough vs `scutil --proxy` vs `NO_PROXY` diff | Low |
| `install`: PATH symlink, plist, bootstrap | `tap install` (repair semantics) | ported | Yes | native | **Legacy `install` relinks PATH `tap` to itself and bootstraps `com.tap`** — now gated | — | — | High if ungated (fixed in legacy `54ccb21`) |
| NOFILE `ulimit` wrapper, label-deregistration wait, squatter detection | `ulimit` wrapper and exact-owned-job bootout in `runtime.py`; no explicit squatter verdict | replaced | Yes · came from real fd exhaustion and a port-squatting incident | native | Legacy `stop_proc` pkills any mitmdump on its port | — | — | Low |
| Bypass list from `directions.json` (`who`/`why`/domains; pinning, legacy-TLS, telemetry, streaming, build repos) applied as OS bypass only | profile `passthrough` (OS bypass **and** `--ignore-hosts`), Core default = Apple hosts only | missing (data) | **Yes — every entry records a real breakage** · agent session 2026-09-15 edited it to unbreak console tools | native (data import) | none — Core's mechanism is strictly better (env-proxy clients are covered too) | Passthrough entries with provenance (`who`/`why`), editable without hand-editing `profile.json`, >64 entries, applied without `off`→`on`; pack-proposed entries. Extends #6 / #147 | a pinned client (e.g. a package index client that rejects the local CA) works with TAP on | Medium: today each breakage must be rediscovered |
| CA generation and manual trust | shared `~/.mitmproxy` CA, `finish-setup` | ported | Yes | native | CA is shared: removing legacy state must not touch it | #193 | — | High if deleted |
| Scoped sudoers rule | `finish-setup` installs Core's own | ported | Yes | native | none | — | `sudo -n -l` shows Core's rule after legacy's is removed | Low |
| Proxy listening on all interfaces, no auth | loopback-only listener | retired | No | drop | **Never carry over** | — | — | — |

## 3. Capture format and storage

| Legacy capability | Core equivalent | Status | Still needed? · evidence | Target | Ownership conflict | Required Core API / contract | Acceptance test | Risk |
|---|---|---|---|---|---|---|---|---|
| Enqueue-only hook, writer thread, drop-and-count, SSE/binary passthrough decided at `responseheaders` | same design in `capture.py` / `journal.py` | ported | Yes | native | — | — | fixtures in `legacy-characterization-2026-09-07.json` | Low |
| Record envelope `{ts, method, url, status, ctype, size, body_kept, streamed, ua, req_body?, body?}` (unversioned) | record v1 (adds `record_id`, reasons, caps); v0 decodes | ported | Yes | native | — | — | mixed-journal tests exist | Low |
| Body kept for any `*json*` / `text/*` ctype (not exact `application/json` — vendor `+json` types were once lost) | same rule, 12 MiB cap | ported | Yes | native | — | — | vendor `+json` fixture | Low |
| Request body of a call whose **response is SSE** (LLM API request history) | `request_body_paths`, including exact-origin-scoped pack declarations (#198) | **ported** | **Yes — the legacy Claude wire reader is built entirely on request bodies** · 104 sessions + 647 contract files on disk | native | — | Capture policy retains allowlisted request bodies independently of response retention; pack paths require `capture.read` and exact origins | tested SSE POST record round-trips through the v1 validator while a non-allowlisted path remains dropped | Low: Claude pack itself remains to be shipped |
| 7 GB segments × 3, single shared `read.offset`, capture resets the reader's cursor on roll (unread tail lost) | 128 MiB × 3, per-reader opaque cursors, gap receipts | replaced | No (the legacy behaviour is a bug) | drop | — | — | #126 | — |
| ≈20 GB of legacy archives in `~/.tap` | none; decoder accepts v0 but the journal only opens numeric-suffix segments inside the profile | missing | Partly. Already projected until 2026-09-17; **unprojected residue: 09-17…09-21 window** for readers without a Core pack, LLM request history, raw micro-versions | native (tool) | Must not be inserted into the live profile journal | `tap reader run --journal-dir <dir> --allow-legacy` or a documented offline import profile; output merge policy per pack | replaying a legacy archive through an installed reader yields the same files as the legacy reader did | Medium: window is lost once archives are deleted |
| Raw stream and mitmdump log world-readable, tokens in URLs | profile-private data dir | retired | No | drop | **Never carry over** | — | — | Cleanup needed before archives are kept long-term |
| No host filtering at capture; exclusion only by OS bypass | passthrough + `binary_paths`; no "intercept but don't record" tier | replaced | — | native | — | #6 four-layer policy | — | Low |

## 4. Mutators, injection, page↔host channel

Most legacy "mutators" are page injection plus small same-origin endpoints, not
traffic rewriting. They map onto `page` + `handler` roles, **except** for the
endpoints.

| Legacy capability | Core equivalent | Status | Still needed? · evidence | Target | Ownership conflict | Required Core API / contract | Acceptance test | Risk |
|---|---|---|---|---|---|---|---|---|
| Mutator loader: glob `mutators/*.py`, fan all mitmproxy hooks, `.active` opt-in, `_` prefix disables | pack store + grants; profile `--addon` escape hatch | replaced | Mechanism no | drop | **A second in-process hook fan-out competes with Core's addon order** | Installable `mutator` role if ever needed: #10, #157 | — | — |
| Unconditional CSP header removal (three sites) | Core adds a nonce to `script-src-elem`; never strips | retired | No | drop | **Never carry over** | — | injected pack script runs under the site's own CSP | — |
| Stripping `If-None-Match`/`If-Modified-Since` so an injected document is never a 304 | bridge strips the same validators on allowed top-level documents (`bridge.py`) | ported | Yes | native | — | — | reload of a cached page still gets the bootstrap | Low |
| `auth-harvest`: copy `Cookie`/`Authorization`/`User-Agent` for declared hosts to `~/.tap/auth` | `session.observe` → `state/packs/<id>/auth/` | replaced (narrower) | **Yes · still read by a running backfill and by every API command** | native | Shared world-of-packs credential dir is retired; per-pack dirs are right | Generalize `session.observe`: more than one origin, available to non-command roles, freshness/expiry signal, and a Core-provided `{proxy, ca_file}` for replay clients. Extends #26 | a pack command makes an authenticated call without hand-configured proxy/CA/auth paths | **High: legacy creds stopped refreshing at retirement** |
| `chatgpt-reveal`: buttons + `GET /__tap/reveal` → Finder reveal of the materialized file | pack `chatgpt.files` (page + handler) | replaced | Yes | pack | — | — | reveal works on a conversation archived by `chatgpt.sessions` | Low |
| `youtube-copy-links` + caption side-fetch so passive capture sees transcripts | `example.sdk-youtube-copy`, `youtube.subtitles` | replaced | Yes | pack | — | — | copy on a card produces a subtitles record | Low (verify the side-fetch survived the port) |
| LinkedIn copy-links shim (already disabled in legacy) | pack `linkedin.copy-links` | ported | Yes | pack | — | — | — | — |
| **Pack-served same-origin routes** `/__tap/<name>` (reveal, transcript POST/GET, blob GET, link repair) | only `/__tap/probe/*` reserved for the bridge; handlers are reachable via `TapBridge.request` (5 s, 256 KiB, JSON) | **missing** | Yes for binary/large payloads (blob serving, 4 MB transcript POST) · 52 transcripts, 181 link files on disk | native (contract) | Routes must be Core-mediated: token + origin check, credential scrubbing, excluded from capture | `handler` sub-contract: routes under `/__tap/pack/<id>/…` with method/size limits and streaming bodies | a pack serves a 1 MB blob to its page; the flow is absent from capture | Medium |
| `site-probe` bootstrap, allowlist, token, credential scrubbing | bridge addon (ported from it) | ported | Yes | native | — | — | `live-slice.md` | — |
| Probe runtime: `TapProbe.registerAction/subscribe/emitEvent`, armed remote eval with handles | `TapBridge.request/expose`, `tap dev execute/inspect` | replaced | Yes | native | One token for page **and** controller planes, readable by any page script — **never carry over** | — | — | — |
| Probe hub: durable journal, cursor replay, outbox, non-page participants | Hub `tap.bridge/v1`: request/result only | missing | Only for the chain below | compat | A second hub process with its own lifecycle (pid files) | handler→page events/subscriptions with a durable per-origin journal; long-running **pack service** entrypoint (today only profile-level `components.services`) | an event emitted while a page is disconnected is delivered on reconnect | Medium |
| DOM adapters `tap.adapter/v1alpha1` (ranked selectors, semantic predicates, hot install by sha) | none; packs ship their own DOM code, `tap.inspector` for discovery | out-of-core | Useful pattern | pack (library) | — | none — hot delivery already works for page packs | adapter fixture tests move with the library | Low |
| Flow executor: Action → Need → dispatch to a capable page → fulfil from an observation | none (deliberately deferred in `live-slice.md`) | missing | **Yes · 28 opened/28 fulfilled Needs journaled; the YouTube → Dópo → ChatGPT chain was in daily use until 2026-09-16** | compat | Coordinator must not live in Core | bridge events + service role (above) | the chain's end-to-end path passes with legacy stopped | Medium: largest compat item |
| Dópo page features (ask-ChatGPT, lineage rail, result render, link repair, transcripts, renderer hot-reprojection), reconciliation job | none | missing | Yes (same evidence) | compat | — | pack routes, pack storage, bridge events | same as above | Note: legacy HEAD is internally inconsistent — a WIP commit re-enabled a monolith and unhooked the renderer package; the chain doc describes the other variant. Port from observed behaviour, not from the chain doc |
| X post adapters and flows | packs `x.ui`, `x.posts` cover the site | retired (as legacy) | No — executors never existed | drop | — | — | — | — |
| `probe-trace` WebSocket trace | none | retired | Dev-only | drop (or Hub debug log) | — | — | — | — |

## 5. Readers, exporters, backfill, commands

| Legacy capability | Core equivalent | Status | Still needed? · evidence | Target | Ownership conflict | Required Core API / contract | Acceptance test | Risk |
|---|---|---|---|---|---|---|---|---|
| ChatGPT versioned archive (`<CID>.<sha8>.json` + flat symlink) | `chatgpt.sessions`; output dir symlinked into `~/tap-out` | ported | Yes · live | pack | — | — | `tap reader status chatgpt.sessions` | Low. Flat symlinks are a contract other tools depend on |
| Codex Work envelopes, readable Markdown, health check (`chats status/refresh/check`) | `chatgpt.work` (`chatgpt work …`) | ported | Yes · live | pack | Legacy `tap chats refresh` still runs standalone and races the pack on the same dirs | — | `chatgpt work status` | Low |
| Organizer: catalog sqlite, auto-rename, own launchd job | `chatgpt.organizer` | ported | Yes · live | pack | Legacy wrote its own LaunchAgent — replaced by background host | — | `chatgpt organizer status` | Low |
| `chatgpt search` | pack `chatgpt.search` released, **not installed** | missing (install) | Yes | pack | — | — | `tap chatgpt search <q>` returns results | Low |
| `chatgpt chat fork`, `chat rename`/`actualize`, `work rename` | none | missing | Yes · were the agent-facing write commands | pack | Legacy commands default to the dead proxy port and the frozen credential dir | generalized `session.observe` (§4) | each command round-trips against a scratch conversation | Medium |
| Historical ChatGPT backfill (separate repo; one request per ~12 min) | none. **Still running from a terminal, replaying the frozen legacy credentials through Core's proxy** | missing | Yes · 1000 done / 44 pending on 2026-09-21; see #188 | pack (scheduled command) | Reads credentials from the retired dir | `session.observe` for a scheduled command; pack→pack read of already-archived ids | backfill continues after the legacy credential dir is removed | **High: will start failing when the bearer expires** |
| ChatGPT file/sandbox blob grabbing, linked copies, bundles, lost-message diff (`retro/`) | none (`chatgpt.files` only reveals/copies paths) | missing | Partly · last run 2026-08-01; outputs still indexed downstream | pack | Token mined from capture bodies, hand-pasted cookie file, secrets on curl argv — **never carry over** | pack blob store convention (sha-addressed + manifest); `binary_paths` for passively seen downloads | a conversation with an attachment is exportable as a self-contained bundle | Low |
| Claude wire reader: per-branch session history + deduped request "contract" | none | **missing** | Yes · 104 sessions / 647 contracts, stale since 2026-09-07 | pack (`claude.sessions`) | — | **request-body retention policy (§3)** | branch history reproduces legacy output on the legacy fixture | High (blocked) |
| Claude disk relay → `~/tap-out/claude/sessions` | none. **Runs as `com.tap.claude-disk-source` from an untracked script stored inside the output archive**; no versions, skips all 104 wire-era sessions | missing | **Yes · feeds session search daily** | pack (scheduled command, like `cursor.sessions` / `kimi.sessions`) | A `com.tap.*` LaunchAgent outside Core's lifecycle | none | pack output is a superset of the script's; LaunchAgent removed | Medium: single untracked file is load-bearing |
| Claude readable Markdown | none | missing | Yes | pack (same) | — | — | — | Low |
| YouTube details + subtitles | `youtube.subtitles` | ported | Yes | pack | — | none. **Path continuity:** pack writes to its own data dir; `~/tap-out/youtube` (≈1750 videos) is stale and not linked — fold into #184 | consumers of the archive path see new videos | Medium: silent staleness |
| LinkedIn entity pool (overwrite by URN) | `linkedin.archive` (versioned sqlite, narrower kinds) | replaced | Yes | pack | — | decide kinds to widen; optional import of legacy files | — | Low |
| Mail-thread HTML reader | none | missing | Unclear · output stale since 2026-09-08; a separate CLI covers the service | pack or drop | — | — | decision recorded | Low |
| Government-portal activity reader + calendar feed export | none | missing | Unclear · stale since 2026-08-25 | pack or drop | — | — | decision recorded | Low |
| `sink/` push to the publishing site | none | out-of-core | belongs to that site's tooling | drop from TAP | — | — | — | — |
| `dig/` OpenAPI inference over the stream | none | out-of-core | dev tooling | unknown | — | bounded journal scan for commands (no pull/seek API exists for non-reader roles) | — | Low |
| Gen-1 `readers/` + Go `orchestrator/`, `read/assemble|join|verify|source_disk` | — | retired | No (superseded; depend on dirs outside the repo) | drop | — | — | — | — |

## 6. Background jobs

| Job | Active now | Replacement | Action |
|---|---|---|---|
| `com.tap` recorder | no (disabled) | Core profile job | keep disabled; delete with legacy |
| `com.tap.read` reader loop | no (disabled) | per-pack reader loops | same |
| `com.tap.chatgpt-organizer` | no | `chatgpt.organizer` background command | none |
| `com.tap.claude-disk-source` | **yes** | none yet | port to a pack, then remove (§5) |
| ChatGPT backfill (terminal process) | **yes** | none yet | port to a pack command; interim: point it at a pack-owned credential dir (§5) |
| Probe hub + sources (pid files) | no | Hub under the components controller | only needed by the compat chain (§4) |

## 7. Protocol and declaration layer

`tap.intent/v1`, CurrentChain, Atlas, Migration and Relations documents have
**no runtime consumer**; they are design documents addressed to coding agents.
Flow (`tap.bus/v1`), Adapter, Renderer, Probe and Reconciliation declarations
have running code. Status: **out-of-core** for the documents (they stay in the
legacy repo or move into the compat pack's `decl/`), **compat** for the
executable ones. Two concept atlases, `implementations/`, the Go orchestrator
and the triples tooling (its facts describe port 8899 ownership) are **retired**.

## 8. Never carry over

1. Any legacy verb that touches the system proxy, launchd, PATH or the CA
   (`on`, `off`, `reload`, `install`). Gated in legacy since `54ccb21`.
2. Port-agnostic `armed()`/`disarm`/`pkill` — they act on whoever owns the proxy.
3. A proxy listening on all interfaces.
4. Unconditional CSP removal; double injection by independent hooks.
5. One shared token for page and controller planes, embedded in page HTML.
6. A shared reader offset that the capture process resets.
7. A global credential directory shared across integrations; credentials
   mined from captured bodies; secrets on process argv; hand-pasted cookie files.
8. World-readable raw capture and proxy logs.
9. Integrations that write their own LaunchAgents or pid-file daemons.
10. A second hook fan-out inside the proxy process.

## 9. Compatibility pack direction

`legacy.tap` is a **family of ordinary packs**, not one umbrella pack and not a
runtime. None of them owns the system proxy, launchd lifecycle, CA install,
capture routing or global status.

| Pack | Roles | Carries | Blocked on |
|---|---|---|---|
| `claude.sessions` | command + background (disk), later reader (wire) | disk relay with versions, readable Markdown; wire branch history + contracts | disk: nothing. wire: request-body retention |
| `chatgpt.api` (or extend `chatgpt.organizer`) | command | fork, rename, work rename; install `chatgpt.search` | generalized `session.observe` (works today with per-pack config) |
| `chatgpt.backfill` (or a `chatgpt.sessions` command) | command + background | slow historical pull | `session.observe` for scheduled commands — available today |
| `chatgpt.files` extension | handler + command | blob grabbing, linked copies, bundles, lost-message diff | blob-store convention |
| `legacy.chain` | page + handler (+ service) | the YouTube → Dópo → ChatGPT chain: page UI, transcripts, links, reconciliation, flow executor, adapters | pack routes; bridge events; pack service role |
| small readers | reader | mail-thread HTML, government-portal activities | decision whether still wanted |

## 10. Missing Core APIs / contracts (ordered by what they unblock)

1. ~~**Request-body retention policy** independent of response retention — #198.~~ Implemented with profile and exact-origin-scoped pack declarations.
2. **Generalized `session.observe`** + Core-provided replay client settings — #199.
3. **Pack-served same-origin routes** under a Core-mediated prefix — #200.
4. **Legacy/foreign journal replay** for installed readers — #201.
5. **Passthrough with provenance**, editable live, pack-proposable — #202 (extends #6).
6. **Bridge events**: handler→page push, subscriptions, durable journal — #203.
7. **Pack `service` role** (versioned, integrity-checked long-running process) — #204.
8. **One-command reload/recovery** (#119).
9. **Installable `mutator` role** (#10, #157) — lowest priority: no surveyed
   legacy behaviour strictly needs it once 3 exists.
10. Pack-contributed `where`/`doctor` entries (#96).

Doc drift noticed on the way: [profile-bridge.md](profile-bridge.md) says CSP is
left intact while `bridge.py` adds a nonce to `script-src-elem`;
[capture-records.md](capture-records.md) previously said a pack could supply
`binary_paths`; #198 now documents that only profiles can declare those. The pack table in [components.md](components.md)
lags the installed set; the legacy CLI hash pinned in
[legacy-cli-lifecycle.md](legacy-cli-lifecycle.md) no longer matches (the
retirement gate changed the script).

## 11. High-value workflow smoke ledger

This is the closure ledger for #194. A row is green only when a repeatable
command and durable evidence exist. It does not make a missing capability
"partially ported": future workflows remain owned by their implementation
tickets in §12.

| High-value workflow | Reproducible smoke | Durable evidence | State |
|---|---|---|---|
| Start, adopt all services, proxy traffic, browser access, rollback to direct | `python3 tools/check_migration_lifecycle.py --allow-system-routing --profile <profile> --tap <checkout>/tap --browser <chromium>` | [`results/migration-lifecycle-20260921T180934Z.json`](results/migration-lifecycle-20260921T180934Z.json) | live pass |
| HTTPS is decrypted by this profile CA and trusted by default curl (no `-k`) | `tap doctor --output raw-json` | [`results/https-trust-20260921T182615Z.json`](results/https-trust-20260921T182615Z.json) | live pass; browser evidence stays separate in the lifecycle row |
| Capture record, rotation/gap semantics and reader delivery | `python3 -m unittest tests.test_capture_storage tests.test_records_journal tests.test_readers tests.test_hubless_readers` | [`results/reader-delivery-parity-2026-09-07.json`](results/reader-delivery-parity-2026-09-07.json) | fixture pass |
| Page injection, Hub/control command round-trip and origin isolation | `python3 tools/check_live_slice.py --help` (run with its declared Bun/Chrome/backend arguments) | [`../live-slice-2026-09-07.json`](../live-slice-2026-09-07.json) | live synthetic pass |
| Installed page-pack update/rollback and hot assets in a browser | `python3 tools/check_hot_pack_assets.py --help` | [`results/hot-pack-assets-live.json`](results/hot-pack-assets-live.json) | live browser pass |
| Linked page → handler → reader workflow | `python3 tools/check_linked_pack_live.py --help` | [`results/linked-pack-live-browser.json`](results/linked-pack-live-browser.json) | live browser pass |
| Daily materializers and ChatGPT background work under Core ownership | `tap reader status chatgpt.sessions` and `tap doctor --output raw-json` | [`results/parity-live-20260921T183053Z.json`](results/parity-live-20260921T183053Z.json) | live pass |

The final row covers the currently enabled high-value readers
(`chatgpt.sessions`, `linkedin.archive`, `usage.meters`, `x.posts`,
`x.subtitles`, `youtube.subtitles`) and the `chatgpt.work` /
`chatgpt.organizer` background jobs. Page features whose actual user action is
still missing are not covered by registry presence; their acceptance remains on
#206, #208 and #209.

## 12. Follow-up tickets

| Ticket | What | Blocked on |
|---|---|---|
| #198 | Capture: request-body retention | — |
| #199 | `session.observe` generalization + replay client settings | — |
| #200 | Pack-served same-origin routes | — |
| #201 | Legacy/foreign journal replay | — |
| #202 | Passthrough with provenance (legacy bypass table import) | — |
| #203 | Bridge events + durable journal | — |
| #204 | Pack `service` role | — |
| #205 | Pack `claude.sessions` (replaces the live LaunchAgent) | wire part: #198 |
| #206 | ChatGPT command parity (search, fork, rename, work rename) | nicer with #199 |
| #207 | ChatGPT historical backfill as a pack command | — |
| #208 | `chatgpt.files`: blobs, linked copies, bundles | — |
| #209 | `legacy.chain` compatibility pack | #200, #203, #204 |
| #210 | Decide: port or retire the two small readers | — |
| #211 | Final retirement checklist | all of the above |

Already tracked elsewhere: reload/recovery #119, mutator role #10/#157, policy
layers #6, `where` #96/#100, status/doctor truthfulness #189–#193, archive
layout #184.

## 13. Rollback and retirement

Legacy stays on disk as reference and rollback until the rows above marked
`missing` with target `pack`/`compat` are `ported` or explicitly `retired`.
Rollback means `TAP_LEGACY_FORCE=1 tap-legacy on` **after** `tap off` — never
both. Before the final deletion: move the unprojected capture window through
replay (§3), port the two live jobs (§6), then remove `~/.tap` raw archives and
`/tmp/tap*.log` (they contain credentials), the `.disabled` plists and the
legacy sudoers rule. Keep `~/.mitmproxy`.
