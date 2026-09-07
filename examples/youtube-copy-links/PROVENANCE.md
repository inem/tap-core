# Provenance and license review

The owner requested extraction of the generic lower layer from their private
TAP work into the MIT-licensed `inem/tap-core` repository. This transfer is
limited to the two example artifacts below. It is not a license claim about the
rest of the private tree, future packs or YouTube itself.

| Artifact in this repo | Reviewed source snapshot | SHA-256 of reviewed bytes | Attribution |
| --- | --- | --- | --- |
| `youtube-ui.js` | Local path `~/Code/youtube-ui.js/youtube-ui.js` (git checkout with **no commits**; content hash is the pin) | `77317dfe7a6708eb0d96ce465ce619aefb5a226b4c4ebaa8c16c78540cc467ed` | No third-party copyright/license header in the file. Local ownership and authorship are the repository owner, Ivan Nemytchenko. |
| `copy-links.js` | Bootstrap string inside private `inem/tap` `mutators/youtube-copy-links.py` at commit `959ec9535df3ea848c9c2e6fabf06f1cc014ac24` (author Ivan Nemytchenko \<nemytchenko@gmail.com\>). Whole-file SHA-256 of that mutator snapshot: `cfafebda1e78e615360ce531ee9d2d9f272e49270ceaed21324bea4c2de4bff0`. | `2465181b3fb85ceeb4ca22fa678cbac31c97cfc2f5a394c259baa99089786acf` | Same owner attribution. The published file is only the bootstrap body, not the mutator’s CSP-stripping injector. |

The published copies match those hashes byte-for-byte. No private traffic,
credentials, account data or site cookies are included.

## License decision

Both reviewed sources carry no separate third-party LICENSE/COPYING/NOTICE and no
embedded third-party copyright headers. Inspected authorship is the repository
owner. Under the owner’s requested extraction into `tap-core`, these two
artifacts are published under this repository’s MIT license
(`LICENSE`, Copyright (c) 2026 TAP Core contributors).

No third-party runtime is vendored here. YouTube page DOM is not redistributed;
the scripts only target it at runtime in the user’s browser.

## Delivery change (not a source change)

| Before | After |
| --- | --- |
| Legacy mitmproxy mutator served concatenated library+bootstrap at `/__tap/youtube-copy-links.js` and removed CSP | Profile bridge `page_scripts`: `youtube-ui.js` then `copy-links.js` as `core/0.js` / `core/1.js`; CSP preserved; nonce reused |

Scripts are snapshot inputs at profile `on`, not a hot mutator asset path.
