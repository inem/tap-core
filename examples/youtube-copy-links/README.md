# YouTube Copy Links pack for TAP

An independently released example pack for
[TAP Core](https://github.com/inem/tap-core). It adds a **Copy** action to
YouTube cards, watch pages and Shorts and writes a `https://youtu.be/<id>` URL
to the clipboard with visible toast/button feedback. The installed live evidence
currently covers the watch-page path only; cards and Shorts remain fixture/source
expectations.

The repository is intentionally both a pack and a page-resource provider:

- `tap-resource.json` publishes `youtube.ui@0.1.0` under
  `tap.page-resource/v1`;
- `pack.json` freezes that provider plus the feature resource and declares an
  ordered `uses` list;
- TAP installs the verified bytes into its profile-local shared resource store;
- when several enabled packs use the same ID, version and hash, TAP injects it
  once per document. Version or byte conflicts fail before page code runs.

GitHub distributes source and release artifacts; the browser runtime never
fetches GitHub, npm or a CDN. The `.tap-pack` is self-contained so install and
rollback continue to work offline.

## Files

| File | Role |
| --- | --- |
| `youtube-ui.js` | Reusable DOM adapter exposed as `window.YouTubeUI` |
| `copy-links.js` | Copy-links feature bootstrap |
| `tap-resource.json` | Standalone `youtube.ui` provider declaration |
| `pack.json` | Pack identity, access requests, frozen resources and ordered uses |
| `PROVENANCE.md` | Source hashes, attribution and license review |
| `tools/check_seams.py` | Offline bridge/injection seam check |
| `tools/check_live.py` | Fresh-profile public YouTube check |

## Install the prerelease

Download the `.tap-pack` from
[v0.1.0](https://github.com/inem/tap-pack-youtube-copy-links/releases/tag/v0.1.0),
then, with the TAP profile stopped:

```sh
./tap --profile /absolute/profile pack install \
  /path/to/example.youtube-copy-links-0.1.0.tap-pack
./tap --profile /absolute/profile pack enable example.youtube-copy-links \
  --version 0.1.0 \
  --grant-origin https://www.youtube.com \
  --grant-origin https://youtube.com \
  --grant-capability page.inject
./tap --profile /absolute/profile on
```

The profile's base bridge must be enabled. It does not need source-checkout
paths or the pack origins: the installed pack contributes both to the effective
startup plan.

## Validate from source

Use a TAP Core checkout that contains `tap.page-resource/v1`:

```sh
PYTHONPATH=/absolute/path/to/tap-core \
  python3 -B -m tap_core.page_resources .
PYTHONPATH=/absolute/path/to/tap-core \
  python3 -B -m tap_core.packs .
PYTHONPATH=/absolute/path/to/tap-core \
  python3 -B tools/check_seams.py
```

Builds are deterministic:

```sh
PYTHONPATH=/absolute/path/to/tap-core \
  python3 -B -m tap_core.pack_store build . \
  --output /tmp/example.youtube-copy-links-0.1.0.tap-pack
```

## Evidence and limits

The [installed-artifact live report](evidence/youtube-installed-live-2026-09-07.json)
records Chrome 152 loading immutable profile paths on public YouTube and the Copy
action producing the expected short URL. That run ignored browser certificate
errors and encountered no source nonce, so it does not establish system CA trust
or live nonce reuse. The scripts preserve CSP and the fixture covers nonce reuse.

The Copy path supplies SVG DOM nodes to `youtube-ui.js`, so it does not execute
the library's generic string-icon `innerHTML` branches. Those branches remain
untested under YouTube Trusted Types and require separate evidence or a provider
revision before another pack should rely on them.

All injected scripts share the authority of the YouTube document. This contract
provides deterministic composition and integrity, not a JavaScript sandbox.

## License

MIT. See [PROVENANCE.md](PROVENANCE.md) for the migrated source review.
