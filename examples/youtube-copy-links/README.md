# YouTube copy-links — first site example

Current working feature from the trusted legacy TAP mutator
(`mutators/youtube-copy-links.py`) plus `youtube-ui.js`, delivered through the
profile bridge (`page_scripts`) instead of a custom mutator route.

**Action:** Copy → `https://youtu.be/<id>` on cards / watch / Shorts, with toast
and button flash. Caption fetch remains a page-side side effect for capture; it
is not required for the visible Copy result.

The source now has a `pack.json` for the first installed
`browser-scripts-v1` binding. Hub auto-start (#11/#32) is not required for Copy
itself, but the bridge still injects `runtime.js` before the pack scripts.

## Files

| File | Role |
| --- | --- |
| `youtube-ui.js` | Host DOM adapter (`window.YouTubeUI`), copied as-is |
| `copy-links.js` | Feature bootstrap, extracted from the legacy mutator |
| `bridge.json.example` | Shape only; absolute paths are filled by the seam check |
| `PROVENANCE.md` | Content hashes, ownership and license review |

## Evidence status

| Evidence | Status | Owner |
| --- | --- | --- |
| Fixture seam admit (`tools/check_youtube_copy_links_seams.py`) | Covered by this change | this example |
| Live installed injection + Copy with visible feedback | Automated pass; [report](../../docs/youtube-installed-live-2026-09-07.json) | #14 / #38 |
| System CA trust + nonce-bearing live response | Open; test browser bypassed cert errors and this response had no source nonce | #38 |
| Immutable pack build/install/enable | Covered by the #14 lifecycle slice | #14 |
| Hub auto-start | Covered separately | #11 / #32 |

This pack slice must not close #38: the live report proves installed injection
and the Copy interaction, but not system CA trust or nonce reuse.

## Wire to a profile

```sh
# from this repository root, after choosing an isolated profile + backend
python3 tools/check_youtube_copy_links_seams.py \
  --proxy-port 19001 --hub-port 19002 --write-config /tmp/yt-bridge.json

./tap --profile /absolute/profile install \
  --backend /absolute/path/to/mitmdump --port 19001 --routing explicit \
  --bridge-config /tmp/yt-bridge.json

./tap --profile /absolute/profile bridge explain --origin https://www.youtube.com
./tap --profile /absolute/profile on
```

Point the browser (or a dedicated profile) at the proxy. Do not use the live
user capture journal as test state.

For the installed path, build the artifact and use `tap pack install` / `enable`
as documented in [the pack lifecycle](../../docs/pack-lifecycle.md). The profile
then contains immutable script paths and separate user grants; no bridge config
needs paths back into this source directory.

## Seams this example must prove

Fixture checks, live HTTPS and clean-Mac acceptance are different evidence.

1. **Config admit** — `allow_origins` exact `https://www.youtube.com` and
   `https://youtube.com`; both scripts absolute, ≤256 KiB, UTF-8; `hub_port`
   ≠ proxy port (seam check takes `--proxy-port` and rejects collisions).
2. **Script order** — `youtube-ui.js` then `copy-links.js` as `core/0.js` /
   `core/1.js`. Bootstrap no-ops without `YouTubeUI`.
3. **Injection vs legacy mutator** — bridge keeps CSP and reuses page nonce;
   legacy stripped CSP. Nonce/CSP on real YouTube is the first live gate.
4. **Asset serving** — `core/N.js` from bridge memory with token; `runtime.js`
   still forwarded to Hub. Copy must work even when Hub is down or ignored,
   provided classic scripts still execute after a failed runtime load.
5. **TLS trust** — browser accepts the profile MITM certificate for YouTube
   HTTPS. Untested by loopback fixtures.
6. **SPA** — after one HTML inject, `yt-navigate-finish` / card observers keep
   mounting without reinjecting the document.
7. **Policy scope** — `bridge explain` allows injection/route only; it does not
   claim capture, TLS or app-scope policy (#3/#6).
8. **Visible result** — clipboard + toast/flash on a real watch or card click.

Optional later seam (not v0 of this example): caption `/youtubei` + timedtext
appear in capture → reader projection → page command through Hub.
