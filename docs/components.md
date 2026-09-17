# TAP components map

One page that answers "what is what" across the TAP ecosystem: which repository
owns which piece, what an installed instance looks like on disk, which command
exercises which component, and what was actually verified live. It complements
the product framing in [README.md](../README.md) and the per-slice documents in
this directory; it does not replace either.

Snapshot date of the "verified" columns: **2026-09-16**.

## Repositories

| Repository | Role | State |
|---|---|---|
| [`tap-core`](https://github.com/inem/tap-core) | The runtime: capture, routing, readers, page bridge/Hub, pack lifecycle, commands, diagnostics. **This is the current TAP.** | public · CI green · release tracker [#1](https://github.com/inem/tap-core/issues/1) |
| [`tap-runtime`](https://github.com/inem/tap-runtime) | Public runtime boundary and contracts (`contracts/`, `docs/`, `fixtures/`). | public · bootstrap only |
| [`tap-pack-sdk`](https://github.com/inem/tap-pack-sdk) | Author tooling for packs: `sdk.py init/build/check`, generates `pack.json`, hashes, README, `.tap-pack` archive. | public · v0.1.0 |
| `tap-pack-*` | One repository per pack, released as GitHub releases; installed with `tap pack add OWNER/REPO[@VERSION]`. See the pack table below. | mixed public/private |
| [`tapout-cli`](https://github.com/inem/tapout-cli) | `tapout`: index + search over the `~/tap-out` archive that readers produce (ChatGPT web, Codex Work, Claude Code, Kimi). The first tool to reach for when looking for an old conversation. | private |
| [`tap-unfolded`](https://github.com/inem/tap-unfolded) | Private design world and fold receipts. | private |
| [`tap`](https://github.com/inem/tap) | **Legacy** bash `tap` + mitmdump addon + Elixir/Python readers. Source material and behavioural evidence for the extraction (see [extraction-start.md](extraction-start.md)); superseded by Core for daily use. | private · WIP branch `youtube-intent-chain` |
| [`tap-inject`](https://github.com/inem/tap-inject), [`apitap`](https://github.com/inem/apitap) | Early satellites: mutator catalogue for injecting into live traffic; OpenAPI inference from captured traffic. | private · dormant |

## Installed instance layout

The installer ([install.md](install.md)) or a development checkout
([runtime.md](runtime.md)) produces one *install root* per instance:

```text
<install-root>/
  bin/tap            wrapper: exec <checkout>/tap --profile <profile> "$@"
  checkout/          git clone of tap-core (may be pinned; may carry local patches — diff it against origin/main)
  checkout.before-*  previous checkouts kept by `tap update` for rollback
  profile/
    profile.json     backend, port, routing, bridge (hub_port, allow/exclude origins), components (readers/handlers/services)
    packs/<id>/versions/<ver>/   immutable pack versions: pack.json, BUILD.json, handler.py, reader.py, page.js, README, LICENSE
    state/           capture.json, components.json, bridge.json, pack-registry.json, effective-runtime.json, tokens, checkpoints
    data/readers/<id>/            reader outputs (what `tap-out` symlinks point at)
    logs/            capture.log, components.log, background-host.log, handlers/<id>, services/<name>
    certificates/    profile CA (mitmproxy-ca-cert.pem)
```

Processes (launchd, one label per instance): the proxy (`mitmdump` + capture
addon), the **component controller** (`tap_core/service.py`: starts the Hub,
reader loops and managed services), the **Hub** (`tap_core/hub.mjs` on Bun:
same-origin WebSocket bridge, page registry, controller HTTP API on
`hub_port`), and the **background host** (scheduled pack commands).

## Component → command map

| Component | Module | Exercise it with | Docs |
|---|---|---|---|
| System routing / proxy arming | `routing.py`, `runtime.py` | `tap on`, `tap off`, `tap routing set explicit\|system` | [proxy-routing.md](proxy-routing.md), [process-routing.md](process-routing.md) |
| Capture (records + bodies) | `capture.py`, `records.py`, `journal.py` | `tap status` → capture row; `state/capture.json` | [capture-records.md](capture-records.md), [capture-recovery.md](capture-recovery.md) |
| Readers | `readers.py` | `tap reader status NAME`, `tap reader run\|replay\|rebind NAME --definition …` | [readers.md](readers.md) |
| Page bridge + Hub | `bridge.py`, `hub.mjs`, `page_resources.py` | `tap bridge explain --origin …`, `tap pages` | [profile-bridge.md](profile-bridge.md), [pack-lifecycle.md](pack-lifecycle.md) |
| Page commands (pack-owned ops over WS) | `page_control.py` | `tap page call PAGE_ID OPERATION --args JSON` | [pack-contract.md](pack-contract.md) |
| Development channel | `page_control.py` | `tap dev allow ORIGIN`, `tap dev pages`, `tap dev inspect PAGE_ID SELECTOR`, `tap dev execute PAGE_ID --source …` | [page-development.md](page-development.md) |
| Managed components | `components.py`, `service.py` | `tap components configure --config …`; `state/components.json` | [managed-components.md](managed-components.md) |
| Packs (store, lifecycle, grants) | `packs.py`, `pack_store.py`, `pack_add.py` | `tap pack list\|add\|install\|update\|enable\|rollback\|disable\|uninstall` | [pack-lifecycle.md](pack-lifecycle.md) |
| Installed commands + background | `commands.py`, `background.py` | any `tap <pack path …>` declared by a pack; `state/background.json` | [commands.md](commands.md), [background-commands.md](background-commands.md) |
| Diagnostics | `status_view.py`, `doctor_view.py`, `where_view.py`, `projection.py` | `tap status [--output semantic-json]`, `tap doctor`, `tap where` | [status-output.md](status-output.md), [doctor-output.md](doctor-output.md), [where-output.md](where-output.md) |

## Pack roles

A pack declares one or more entrypoints; Core routes each to the matching
component. Grants (`access.origins`, `access.capabilities`) are requested by the
manifest and confirmed at `pack enable` / `pack add --yes`.

| Role | Entrypoint | Runs in | Reaches the user through |
|---|---|---|---|
| page | `page` (`browser-scripts-v1`, resources under `tap.page-resource/v1`) | the allowed origin's document, injected by the bridge | DOM / UI on the site |
| handler | `handler` (`python-jsonl-v1`) | a Hub child process per request | `TapBridge.request()` from the page → JSON result |
| reader | `reader` (`python-jsonl-v1`, optional `fresh-and-replay-v1`) | reader loop under the controller | files under `data/readers/<id>` |
| command | `command` (`process-argv-v1`) | the CLI wrapper, optionally scheduled by the background host | `tap <path…>` |

## Live check recipe (WebSocket inspection)

The `tap.inspector` pack exposes two read-only page operations over the same
Hub transport every page pack uses. Together with the Core development channel
this is enough to verify, from the terminal, which packs are injected on a page
and whether the bridge round trip works:

```bash
tap status && tap doctor                              # proxy, capture, bridge, controller, readers
tap pages                                             # connected pages: page id, origin
tap page call PAGE_ID tap.inspector.describe --args '{}'                      # url, title, readyState, viewport, counts
tap page call PAGE_ID tap.inspector.query --args '{"selector":"a[href]","limit":5}'
tap dev inspect PAGE_ID 'main' --limit 5             # Core-owned bounded DOM projection
tap dev execute PAGE_ID --source 'return TapBridge.status()'                  # mode, plan, injected packs + features
tap pack list | jq '.packs | map_values({selected, enabled, grants})'
```

Page ids are volatile: they belong to one WebSocket session, and a browser that
sleeps background tabs drops and re-establishes those sessions. `page_not_found`
after a listing therefore usually means "list again", not a Hub failure.
`operation_unavailable` means the operation's pack is not injected on that
origin (no grant), `operation_failed` means the page-side implementation threw.

## Installed packs (verified 2026-09-16 on one system-routing profile)

| Pack | Selected | Roles | Origins | Source / release | Live check |
|---|---|---|---|---|---|
| `tap.inspector` | 0.3.10 (0.3.11 installed, unreleased) | page + handler | chatgpt, linkedin, youtube, x | [tap-pack-inspector](https://github.com/inem/tap-pack-inspector) v0.3.10 | describe/query ok on all four origins |
| `tap.intake` | 0.7.8 | page + handler | `*` | [inform](https://github.com/inem/inform) (private) — 0.7.8 on main, no release | injected on every connected page; no features declared |
| `chatgpt.files` | 0.1.2 | page + handler | chatgpt | [tap-pack-chatgpt-files](https://github.com/inem/tap-pack-chatgpt-files) v0.1.2 | injected, 2 features |
| `chatgpt.sessions` | 0.3.2 | reader | chatgpt | [tap-pack-chatgpt-sessions](https://github.com/inem/tap-pack-chatgpt-sessions) v0.3.2 | reader waiting · last batch ok |
| `chatgpt.organizer` | 0.6.0 | command + background + session.observe | chatgpt | tap-pack-chatgpt-organizer (private) v0.6.0 | scheduled every 30 s |
| `chatgpt.work` | 0.3.2 | command + background | chatgpt | tap-pack-chatgpt-work (private) **v0.3.1** — installed version ahead of release | scheduled every 30 s |
| `linkedin.archive` | 0.1.1 | reader | linkedin | [tap-pack-linkedin-archive](https://github.com/inem/tap-pack-linkedin-archive) v0.1.1 | reader waiting · last batch ok |
| `linkedin.copy-links` | 0.4.17 | page | linkedin | [tap-pack-linkedin-copy-links](https://github.com/inem/tap-pack-linkedin-copy-links) v0.4.17 | injected, 3 features |
| `x.ui` | 0.7.9 | page | x | [tap-pack-x-ui](https://github.com/inem/tap-pack-x-ui) v0.7.9 | injected, 4 features |
| `x.posts` | 0.2.2 (0.2.3 installed, unreleased) | reader + handler | x | [tap-pack-x-posts](https://github.com/inem/tap-pack-x-posts) v0.2.2 | reader waiting · injected, 2 features |
| `x.subtitles` | 0.2.2 | reader + page + handler | x, video.twimg | [tap-pack-x-subtitles](https://github.com/inem/tap-pack-x-subtitles) (private) — source recovered 2026-09-17, no release yet | reader waiting · injected, 1 feature |
| `x.chat-copy` | 0.1.0 | handler | x | [tap-pack-x-chat-copy](https://github.com/inem/tap-pack-x-chat-copy) (private) — source recovered 2026-09-17, no release yet | injected, 1 feature |
| `youtube.subtitles` | 0.1.1 | reader | youtube | [tap-pack-youtube-copy-links](https://github.com/inem/tap-pack-youtube-copy-links) `packs/subtitles`, release tag `youtube.subtitles v0.1.1` | reader waiting · last batch ok |
| `example.sdk-youtube-copy` | 0.1.0 | page | youtube | [tap-pack-sdk](https://github.com/inem/tap-pack-sdk) example | injected |
| `google.ui` | 0.1.1 | page | google | [tap-pack-google-ui](https://github.com/inem/tap-pack-google-ui) v0.1.1 | not connected during the check |
| `yandex.weather-ui` | 0.1.0 | page | yandex | [tap-pack-yandex-weather-ui](https://github.com/inem/tap-pack-yandex-weather-ui) v0.1.0 | not connected during the check |
| `kimi.sessions` | 0.1.1 (0.1.4 installed) | command + background | kimi | [tap-pack-kimi-sessions](https://github.com/inem/tap-pack-kimi-sessions) (private) — 0.1.0–0.1.4 as commits, no release | injected shell only; command scheduled every 120 s |

Not installed on this profile but released: [tap-pack-chatgpt-search](https://github.com/inem/tap-pack-chatgpt-search)
v0.1.3 (first command provider), [tap-pack-youtube-copy-links](https://github.com/inem/tap-pack-youtube-copy-links)
v0.1.1 (reference external page pack), [tap-pack-linked-http](https://github.com/inem/tap-pack-linked-http)
v0.1.0 (controlled HTTP → reader → handler → page example).

## Known gaps found during the check

- Managed services shared fate with the Hub: any misbehaving pack service killed page injection everywhere. [#140](https://github.com/inem/tap-core/issues/140) → [PR #141](https://github.com/inem/tap-core/pull/141).
- `tap dev execute` fails with a bare `operation_failed` on `www.youtube.com` (Trusted Types / CSP) while `dev inspect` and the inspector operations work. [#142](https://github.com/inem/tap-core/issues/142).
- The hermetic test suite is not hermetic about the proxy port: one installer test refuses to run on a Mac that already runs a profile on 18999. [#143](https://github.com/inem/tap-core/issues/143).
- Three installed packs had no repository (sources sat in per-task Codex/Kimi folders on iCloud); recovered into `tap-pack-x-subtitles`, `tap-pack-x-chat-copy` and `tap-pack-kimi-sessions` on 2026-09-17, still without releases. Two selected versions are ahead of their latest release. Tracked in [#144](https://github.com/inem/tap-core/issues/144).
- System routing used to strip the pinned-client bypass, so iCloud/Apple daemons failed TLS through the proxy ~3×/s and iCloud Drive stopped syncing while TAP was on. [#147](https://github.com/inem/tap-core/issues/147) → the declared `passthrough` list ([proxy-routing.md](proxy-routing.md#pinned-clients-passthrough)).
- `background-host.log` and `components.log` carry no timestamps, which makes "when did the Hub restart" a `ps -o lstart` question. [#145](https://github.com/inem/tap-core/issues/145).
