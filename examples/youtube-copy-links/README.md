# YouTube copy-links — first site example

Current working feature from the trusted legacy TAP mutator
(`mutators/youtube-copy-links.py`) plus `youtube-ui.js`, delivered through the
profile bridge (`page_scripts`) instead of a custom mutator route.

**Action:** Copy → `https://youtu.be/<id>` on cards / watch / Shorts, with toast
and button flash. Caption fetch remains a page-side side effect for capture; it
is not required for the visible Copy result.

This is not an installed pack (#14). Hub auto-start (#11/#32) is not required
for Copy itself, but the current bridge still injects the legacy TapProbe
`runtime.js` tag that expects a Hub on `hub_port`.

## Files

| File | Role |
| --- | --- |
| `youtube-ui.js` | Host DOM adapter (`window.YouTubeUI`), copied as-is |
| `copy-links.js` | Feature bootstrap, extracted from the legacy mutator |
| `bridge.json.example` | Shape only; absolute paths are filled by the seam check |
| `PROVENANCE.md` | Source paths and delivery change |

## Wire to a profile

```sh
# from this repository root, after choosing an isolated profile + backend
python3 tools/check_youtube_copy_links_seams.py --write-config /tmp/yt-bridge.json

./tap --profile /absolute/profile install \
  --backend /absolute/path/to/mitmdump --port 19001 --routing explicit \
  --bridge-config /tmp/yt-bridge.json

./tap --profile /absolute/profile bridge explain --origin https://www.youtube.com
./tap --profile /absolute/profile on
```

Point the browser (or a dedicated profile) at the proxy. Do not use the live
user capture journal as test state.

## Seams this example must prove

Fixture checks, live HTTPS and clean-Mac acceptance are different evidence.

1. **Config admit** — `allow_origins` exact `https://www.youtube.com` and
   `https://youtube.com`; both scripts absolute, ≤256 KiB, UTF-8; `hub_port`
   ≠ proxy port.
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
