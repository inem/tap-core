# Profile-owned page injection and local routing

This bounded #6/#10 slice replaces the generated injector wrapper in #29 with
`tap_core/bridge.py`, loaded by the existing profile runtime. The legacy Hub and
page runtime still supply the wire protocol; the installed pack host and Hub
supervision are not implemented here.

## Configuration and commands

An explicit development configuration contains exactly these fields:

```json
{
  "version": 1,
  "enabled": true,
  "hub_port": 19002,
  "allow_origins": ["http://127.0.0.1:19003", "https://example.test"],
  "exclude_origins": ["https://example.test"],
  "page_scripts": ["/absolute/path/to/trusted-fixture-page.js"]
}
```

Origins use the existing fixture contract's exact canonical HTTP(S) shape:
lowercase ASCII host, optional nondefault port, no path or wildcard. IPv6/IDN
configuration is not supported; unsupported traffic is not eligible for bridge
access. Each list has at most 64 unique entries. Page scripts are trusted classic
browser scripts, read into memory on startup, at most 256 KiB per file. This
binding is not a replacement for `browser-module-v1` pack entrypoints. There is
no pack install, module loader, hot upgrade or automatic dependency discovery.

```sh
./tap --profile /absolute/profile install \
  --backend /absolute/path/to/mitmdump --port 19001 --routing explicit \
  --probe-url http://127.0.0.1:19003/record --bridge-config bridge.json
./tap --profile /absolute/profile bridge explain --origin https://example.test
./tap --profile /absolute/profile off
./tap --profile /absolute/profile bridge configure --config changed-bridge.json
./tap --profile /absolute/profile on
```

Configuration is copied into `profile.json`. Editing the original input file
has no effect. `configure` requires the profile service to be unloaded, validates
configuration and enabled scripts, then saves it for the next `on`. A live
configuration attempt fails before changing the profile. Existing profiles that
have no `bridge` field retain their behavior and do not load this addon.

The Hub port must differ from the profile proxy port.

The profile creates one private `state/bridge-token` (0600), retained across
restarts/configuration changes. The local Hub must explicitly use that token and
port; this slice does not launch or reconfigure it. No token is supplied in a
pack manifest or returned by `bridge explain`/status. The injected runtime receives
the token using the existing page protocol, so allowed page code is trusted with
it. The allow/exclusion rules govern the proxy route; they do not reconfigure
the legacy Hub’s direct listener or replace its own authorization policy (#11).
This is not a sandbox or per-pack/per-origin credential isolation model.

## Policy and behavior

The decision order is disabled → user exclusion → explicit allow → not allowed.
An excluded origin cannot gain access merely because it appears in allow_origins.
`bridge explain` reports the decision and states that its scope is page injection
and the reserved local route. It does **not** change whether a connection uses
the proxy, whether TLS is intercepted, or which HTTP bodies capture saves.
Per-application routing is explicitly unsupported by this policy. This is a
concrete subset of #6, not its host/TLS/app policy completion.

For allowed top-level HTML, the addon inserts the existing runtime bootstrap
followed by configured scripts served from memory under `/__tap/probe/core/`.
It scans script attributes (including whitespace, unquoted values and
HTML entities) to retain an existing nonce. This is a bounded tag/attribute
tokenizer, not a browser HTML5 tree builder. Comments and raw-text contexts do
not supply nonce/marker attributes. Encoded nonce/id values are capped at 4096
characters; unsupported declarations, ambiguous script comments and incomplete
markup skip injection and leave the response intact.
Token-bearing bootstrap and script
URLs are absolute URLs from the intercepted request origin, independent of the
document base URL. It leaves CSP intact and prevents another
bootstrap when the marker is present, and invalidates response validators/cache.
Streamed bodies are not read or injected. Capture runs before response injection;
additional trusted addons run afterward. Only this order is defined; arbitrary
mutator priorities/conflict resolution remain #10 work.

Reserved `/__tap/probe/` requests require both an allowed destination origin and
an exact token; duplicate tokens fail. Requests with a mismatching Origin fail,
and WS requires an Origin. Caller-supplied TAP authority headers are removed
only for reserved requests; ordinary site traffic retains these headers.
Cookie, Authorization and Proxy-Authorization and the URL token are removed
before forwarding to the loopback Hub; rejected reserved requests are also
sanitized. User-excluded and disabled routes get 403 rather than reaching the
remote application under the reserved namespace. Ordinary excluded responses
remain untouched. The addon does not grant extra privileges to third-party
Python hooks supplied through `--addon`.

Changes take effect only after stop/configure/start. Stopping the proxy closes
existing proxied WS connections; with the bridge disabled, their reconnects are
denied and new pages receive no bootstrap. Already executed page scripts and
rendered UI are not removed or revoked: reload the page to discard them. Script
files are snapshot inputs for each startup, not immutable installed artifacts.

## Diagnostics and verification

Status/doctor check that `state/bridge.json` belongs to the current service PID
and matches the configured bridge fingerprint/enabled flag. A missing record is
known absence (`healthy: false`); read failures and malformed records produce
`healthy: null` plus `inspection_errors.bridge`, and doctor remains unhealthy.
This verifies addon startup, not live Hub availability; `hub_liveness` is explicitly
`not_checked`.
Hook/runtime errors remain in the profile capture log. End-to-end diagnostics,
per-pack error isolation and failed Hub supervision remain #11/#13/#14.

The [live report](profile-bridge-live-2026-09-07.json) records the same
capture → reader output → WS → page chain as #29, now using this profile addon.
It additionally verifies two allowed origins, a user exclusion that conflicts
with allow, reload without a duplicate bootstrap, live reconfiguration rejection,
and off/configure/on disabling injection. Spaced quoted and unquoted nonce
attributes work under an unchanged CSP. A foreign-origin base URL does not
redirect bootstrap/assets; zero token-bearing requests leave the allowed origins.
The second tab remains unchanged.
The old `site-probe.py` and a generated Python wrapper are no longer needed.

Run `tools/check_live_slice.py` with the explicit backend/Bun/Node/Playwright/Chrome
and trusted legacy source inputs described in [the first live slice](live-slice.md),
using `--output docs/profile-bridge-live-2026-09-07.json`. Only the legacy Hub,
page runtime and adapter runtime are read from that source. Test tools remain
optional development dependencies, not mandatory core runtimes.

Unit tests cover credentials/origins, unsupported authorities, policy precedence,
valid nonce syntax, foreign base URLs and unavailable diagnostic observations,
streaming, nonce/order, duplicate injection, private token persistence, bounded
script loading, compatibility and startup diagnostics. The combined stack passes
178 unit tests, synthetic reader CLI replay and pack fixtures. Live evidence uses
loopback HTTP with synthetic data on macOS 15.6.1 arm64. HTTPS trust, the broader
CSP/browser matrix, third-party WS capture, app routing, package lifecycle and clean-Mac installation
remain unverified by this change. No production capture, Hub journal, browser
profile or system proxy setting is used or changed.

The inspected standalone mitmdump 12.2.3 omits `html.parser`, even though the
development Python provides it. The tokenizer avoids that dependency. Loading
the addon in the actual backend is part of live verification; passing tests in
the development interpreter alone does not establish backend compatibility.
The live report also records the supplied Node runtime (`v24.19.0`) used to run
Playwright; Node remains a development harness dependency.

## Source provenance

The behavioral source is `mutators/site-probe.py`, SHA-256
`a2f85cf20e05e20b3067afcc42e25da9daa069cd9e7b473c42a93db1c7557a85`,
characterized in #29. It carries no third-party copyright/license header;
inspected Git attribution is the repository owner. This transfer falls within
the owner's requested extraction of TAP's generic lower layer into MIT tap-core.
It ports bootstrap/nonce/routing/credential-scrubbing behavior with new explicit
profile configuration, validation, script assets and tests. No private source
snapshot, application declaration, captured data or credential is published.
The Hub/runtime remain explicitly supplied source candidates, not copied here.
