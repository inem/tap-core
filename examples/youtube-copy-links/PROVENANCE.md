# Provenance

| Artifact | Source | Notes |
| --- | --- | --- |
| `youtube-ui.js` | `/Users/inem/Code/youtube-ui.js/youtube-ui.js` (local checkout) | Copied verbatim for the example. No build step. |
| `copy-links.js` | Bootstrap string inside `/Users/inem/Code/tap/mutators/youtube-copy-links.py` | Behavior unchanged. Delivery no longer concatenates library+bootstrap into one mutator-served `/__tap/youtube-copy-links.js`. |
| Delivery | Legacy mitmproxy mutator → tap-core `tap_core/bridge.py` `page_scripts` | CSP is no longer removed. Scripts are snapshot inputs at `on`, not a hot mutator asset path. |

License review for publishing these files under the repository MIT license remains
an extraction obligation (see README / TECHNOLOGY). Private capture data and
credentials are not included.
