# External pack development and lifecycle

This is the first installed slice for #14. It binds independently distributed
page packs to the proven profile bridge and defines the separate
[`tap.page-resource/v1`](../contracts/page-resource/v1/README.md) seam used by
UI-library providers, pack authors and TAP Core. The reference
[YouTube Copy Links pack](https://github.com/inem/tap-pack-youtube-copy-links)
lives in its own repository. Installed `page/browser-scripts-v1` and
`reader`/`handler` `python-jsonl-v1` bindings are in main (#45 / #57); mutator
and `browser-module-v1` activation remain unsupported. The linked capture →
reader → page/handler external example is still open acceptance work.

## The development cycle

The useful loop begins before packaging. These stages deliberately carry
different evidence and authority:

1. **Observe** — use an isolated profile to capture a named site's exact origins
   and record which HTTP bodies, SSE responses and third-party WS frames are
   visible or missing. Capturing traffic does not authorize page injection.
2. **Prototype** — point the development bridge at explicit source scripts and
   iterate on the site adapter/UI. These mutable checkout paths are development
   inputs, not installed-pack evidence.
3. **Verify live** — record the browser/site versions, TLS trust, CSP/nonce result,
   SPA navigation behavior and one visible user action. Fixtures remain separate
   evidence.
4. **Freeze the seam** — select concrete entrypoint interfaces, stable page
   resource IDs and versions, requested exact origins/capabilities, config,
   dependencies and state migration. The manifest requests authority; it never
   grants itself authority.
5. **Build** — create a deterministic `.tap-pack` containing only `pack.json` and
   its declared files. No source checkout path is retained.
6. **Install and grant** — copy an immutable version snapshot under the profile,
   verify every file hash, then record user grants separately. Enable affects the
   next `on`; it cannot hot-patch an already running proxy or already executed UI.
7. **Operate** — update atomically, roll back to a retained verified version,
   disable, and remove code without deleting pack state/data/logs.

The iteration loop may repeat stages 1–3 many times. A distributable pack begins
at stage 4. Passing a source fixture or a live development check is not evidence
that the artifact can be installed or rolled back.

## Perimeter

| Owner | Owns | Does not imply |
| --- | --- | --- |
| Core host | capture records, profile lifecycle, pack store, integrity checks, bridge/handler transport and enforcement at those boundaries | same-user code sandboxing or correctness of site parsing |
| Page-resource provider | stable resource ID, semantic API, immutable versioned bytes, license and source provenance | page access, pack activation or runtime network fetching |
| Pack | site/domain knowledge, readers, page UI, handlers, config/state migrations and extra dependency delivery | authority merely because it appears in the manifest |
| Profile/user | exact grants, exclusions, selected version and retained state/data/logs | that every requested transport is observable |
| Development harness | mutable source bindings and fixture/live evidence | installed artifact or clean-Mac acceptance |

All installed pack code is trusted code running as the user. Exact origin and
capability grants constrain the host surfaces offered to it; they are not a
filesystem or network sandbox. Page scripts sharing one allowed origin also
share the page authority available there.

## First installed binding

`browser-scripts-v1` is an ordered list of declarative classic UTF-8 script
uses. Every use names a stable `id` and exact `version`; a top-level `resources`
provider pins its packaged `file`, SHA-256, license and source revision under
[`tap.page-resource/v1`](../contracts/page-resource/v1/README.md). This matches
the actual #31 bridge rather than pretending the existing `browser-module-v1`
fixture lifecycle has shipped. On install and startup the host:

- verifies each provider declaration and copies its bytes to the immutable
  profile-local store at `resources/page/<id>/<version>/<sha256>.js`;
- re-reads the pack registry and manifest without importing the checkout as a
  Python package;
- verifies the installed file set and SHA-256 hash of every declared file;
- checks that requested origins/capabilities remain covered by separate grants;
- collects declarations from every enabled pack for each exact origin;
- collapses the same resource `id` when version and bytes match, unions its
  origins and injects it once per document;
- preserves every pack's ordered `uses` through deterministic topological
  composition; a cycle on one origin rejects activation before registry save;
- keeps opposite but valid orders on disjoint origins in separate scoped asset
  entries, without injecting either resource twice in one document;
- rejects conflicting versions or bytes under one resource ID before changing
  the active registry;
- snapshots the deterministic per-origin plan into bridge memory under the
  existing authenticated route.

Overlapping pack origins are therefore expected, not an ownership conflict. A
shared library such as `youtube.ui@0.1.0` may be vendored by several YouTube
packs; installation collapses equal provider bytes into one shared profile
object and activation collapses their matching uses into one injection.
Different semantic scripts need different IDs even if their current bytes
happen to match. A matching ID is an author claim of shared identity, not
content-addressing by accident.

GitHub, a registry or a local checkout may be a build-time source for a provider,
but never a browser-time dependency. V1 artifacts are self-contained so install,
rollback and offline startup cannot change when an upstream URL changes. The
provider publishes the reusable UI/site adapter, each pack declares what it uses,
and Core implements validation, storage, composition and origin-scoped delivery.

Disable prevents injection after the next `off`/`on`. It cannot revoke JavaScript
that already ran in an open document; reload that page. Installed readers and
handlers using `python-jsonl-v1` also bind to the existing managed host. Mutator
activation and `browser-module-v1` remain unsupported.

Reader/handler packs require an existing components configuration with an
absolute Python path. Absolute Bun and Hub are required when the page bridge is
**enabled** (runtime.js / WS) or when handlers are projected. Reader-only packs
may run with `bridge.enabled=false` and without Bun/Hub: managed `on`/`off`
starts the component controller and readers only. A components block still
requires a bridge object (`enabled=false` is the hubless shape; `bridge: null`
is rejected). On startup, the host refreshes the effective origins and handler
bindings from enabled immutable pack versions.
The component name is the pack ID; existing reader checkpoints and handler
state/output/log directories remain under their respective `readers/<id>` and
`handlers/<id>` roots. See [managed component protocols](managed-components.md)
for the reader context and handler request/result envelope. A handler receives
`{version, request_id, args}` and returns `{ok, value}` or a typed error; the old
standalone fixture echo format is not the managed handler protocol.

Updating or rolling back a reader with an incompatible saved definition is
refused before changing the selected version. Its checkpoint is retained; this
slice does not implement checkpoint migration or automatic replay. Explicit
replay remains `tap reader replay`.

## Build and use an external pack

Download the self-contained artifact and checksum from the reference pack's
[v0.1.0 prerelease](https://github.com/inem/tap-pack-youtube-copy-links/releases/tag/v0.1.0),
or clone it beside a compatible TAP Core checkout. To build from source:

```sh
PYTHONPATH=/absolute/path/to/tap-core \
python3 -B -m tap_core.pack_store build /absolute/path/to/tap-pack-youtube-copy-links \
  --output /tmp/example.youtube-copy-links-0.1.0.tap-pack

./tap --profile /absolute/profile off
./tap --profile /absolute/profile pack install \
  /tmp/example.youtube-copy-links-0.1.0.tap-pack
./tap --profile /absolute/profile pack enable example.youtube-copy-links \
  --version 0.1.0 \
  --grant-origin https://www.youtube.com \
  --grant-origin https://youtube.com \
  --grant-capability page.inject
./tap --profile /absolute/profile on
./tap --profile /absolute/profile bridge explain \
  --origin https://www.youtube.com
```

The base profile must already have an enabled bridge, but it need not list the
pack origins or source paths. The enabled installed pack contributes those to the
effective startup configuration. `pack list` shows installed versions, selected
version, grants and activation state.

For a compatible new artifact while the profile is stopped:

```sh
./tap --profile /absolute/profile pack update /tmp/example.youtube-copy-links-0.2.0.tap-pack
./tap --profile /absolute/profile pack rollback example.youtube-copy-links
./tap --profile /absolute/profile pack disable example.youtube-copy-links
./tap --profile /absolute/profile pack uninstall example.youtube-copy-links
```

An update is installed before activation. If its requests, dependencies, config
or binding are incompatible, the old selected version remains enabled and no
mixed version is loaded. Rollback selects the previous verified snapshot.
Uninstall requires disable and removes pack code only; shared resource objects
are retained for other installed versions and rollback history. Profile-owned
`state/packs/<id>`, `data/packs/<id>` and `logs/packs/<id>` remain.

## Evidence and remaining work

Unit coverage verifies deterministic artifacts, path traversal rejection,
separate grants, installed-path binding, tamper rejection, atomic update failure,
rollback and retained state/data/logs. It also enables two packs on the same
origin, verifies one shared UI injection, rejects shared-ID version/content
conflicts and checks that feature resources stay scoped to their declared
origins. The YouTube seam fixture continues to verify script order, exact
origins, sizes and injection behavior.

The reference repository's
[2026-09-07 installed-artifact live report](https://github.com/inem/tap-pack-youtube-copy-links/blob/main/evidence/youtube-installed-live-2026-09-07.json)
records a fresh temporary profile loading only immutable installed paths on
Chrome 152 and public YouTube: YouTube records were captured, the Copy action
wrote the expected short URL and its success state was visible. The exact run
count remains in the report because background request volume varies. The
response had a CSP header but no source nonce to reuse. Playwright ignored
certificate errors, so this is not the clean CA-trust result.

An external linked pack ([`inem/tap-pack-linked-http`](https://github.com/inem/tap-pack-linked-http))
now provides reader, handler and page code in a published artifact. The hermetic
Core check, `tools/check_linked_pack.py`, proves a synthetic record submitted to
Writer → installed reader projection → Hub handler → protocol client result,
plus retained checkpoint and a healthy controller restart. Live proxied HTTP
capture and headless browser Load → `#result` (saved projection) are covered by
`tools/check_linked_pack_live.py` / `docs/results/linked-pack-live-browser.json`
on explicit loopback; they do not claim CA trust, system proxy, SSE or
third-party WS.

Still required for #14: observed reader/handler error paths and lifecycle on
that external example, and independent author reproduction. Clean-Mac release
acceptance belongs to #15. System CA trust and a live nonce-bearing YouTube
response remain #38 evidence. Hubless reader-only managed on/off is accepted
for packs without handlers when `bridge.enabled=false`.
