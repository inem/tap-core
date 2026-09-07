# Experimental pack contract, version 1

The contract began as the preparatory #5 result based on runtime commit
`56094c0`. `pack_api: 1` remains experimental. The current host can install an
immutable artifact and bind `page/browser-scripts-v1`; reader, handler, mutator
and `page/browser-module-v1` activation have not shipped.

The source evidence is the legacy executable reader consuming JSONL on stdin,
the Python mutator loader forwarding mitmproxy hooks, and the site-probe/page
runtime forwarding an allowlisted same-origin route to a local handler. The
contract keeps those concrete entry shapes. It does not carry over the legacy
directory discovery, application declarations, shared reader offset, or Hub's
workflow machinery. The implementation and data in these fixtures are new and
synthetic; no private legacy source is distributed with them.

## Validate a pack

From the repository root, using the existing Python development prerequisite:

```sh
python3 -m tap_core.packs fixtures/packs/reader
python3 -m tap_core.packs fixtures/packs/page-bridge
python3 -m tap_core.packs fixtures/packs/reader --host-api 2
```

The first two commands validate without executing code. The last fails clearly
because the pack requires API 1. A valid manifest is not an activation or access
grant. The authoritative executable validator is `tap_core/packs.py`; unknown
fields and versions are rejected to expose typos and compatibility mismatches.

Each pack directory contains UTF-8 `pack.json`, with these required fields:

| Field | Version 1 meaning |
| --- | --- |
| `manifest_version` | Integer `1`, not a string or boolean. |
| `id` | Lowercase identifier of letters/digits separated by `.` or `-`, starting with a letter. Used for profile-local paths. |
| `version` | Exact `MAJOR.MINOR.PATCH` release version. No range or prerelease syntax in this slice. |
| `requires.pack_api` | Exact integer API expected from the future host; fixtures supply `1`. |
| `requires.dependencies` | Array of `{id, version}` declarations for additional executable/package dependencies, with exact release versions. A pack cannot depend on itself. Host inventory must match before activation. No downloads or dependency resolution here. |
| `files` | Unique canonical relative file paths. Every file must exist and resolve inside the pack. Absolute paths, traversal, backslashes and escaping symlinks fail. |
| `entrypoints` | One or more roles from the table below, each `{file, interface}`; the file must be in `files`. |
| `config` | Named settings, each `{type, default}`. Only string, integer and boolean values; unknown overrides and wrong types fail. No expressions or configuration language. |
| `access.origins` | Nonempty list of exact canonical HTTP(S) origins, including a nondefault port when relevant. No wildcard, credentials, path, query or fragment. ASCII DNS names and IPv4 supported here; IPv6/IDN syntax remains future work. |
| `access.capabilities` | Explicit requests from `capture.read`, `response.mutate`, `page.inject`, `bridge.handle`; every declared role needs its capability. |

Host-provided Python, browser JavaScript and the already selected interception
backend are interface prerequisites, not extra pack dependencies. The two
fixtures need no additional packages. A pack adding a helper must declare its
fixed version; bundling, lockfiles, licenses and platform compatibility remain
delivery obligations. A successful inventory match alone does not verify a
dependency's provenance or install it.

## Entry shapes and lifecycle

| Role and interface | Controlled fixture invocation | Future host responsibility |
| --- | --- | --- |
| `reader`: `python-jsonl-v1` | Python executable, UTF-8 JSON objects on stdin; EOF stops it. Fixture emits results on stdout and diagnostics on stderr. | Start outside the proxy hook path, supply allowed records and explicit context, own delivery/checkpoint/replay semantics. Stdout is **not** a durable acknowledgement. |
| `mutator`: `mitmproxy-python` | Import and call the actual `response(flow)` export using small flow fixtures. | Load only after validation and a user grant; pass native backend hooks. Report exceptions and enforce bounded hooks/streaming behavior. In-process code remains trusted. |
| `page`: `browser-module-v1` | Call exported `start({bridge, document})` and `stop({document})` against a document/bridge fixture. | This remains a fixture interface; an installed module loader has not shipped. |
| `page`: `browser-scripts-v1` | Ordered `scripts` declarations, each with stable `id`, exact `version` and packaged `file`. | First installed binding: verify declarations, merge all enabled packs per origin, deduplicate identical resources and snapshot the resulting plan at startup. Disable applies after restart; an already-open page must reload. |
| `handler`: `python-jsonl-v1` | Python executable consuming the example's JSON request and returning JSON reply; EOF stops it. | Connect a bounded adapter to the local bridge. The Python fixture does not replace the existing Bun Hub or select its eventual execution topology. |

Roles are independent: a page-only, handler-only or reader-only pack is valid.
The second fixture deliberately combines its HTML mutator, page and handler to
exercise the connection shape. This does not require a local handler for every
page injection or claim that every combination has been integrated.

The host supplies `TAP_PACK_CONTEXT` as a JSON string to Python entrypoints:
`pack_api`, `pack_id`, `config`, and three absolute host-owned paths:

- `<profile>/state/packs/<id>` for private mutable state;
- `<profile>/data/packs/<id>` for outputs;
- `<profile>/logs/packs/<id>` for diagnostics.

These paths are independent of the installed code directory and pack version.
`fixture_context()` only computes paths/configuration; it creates nothing. The
host creates private profile directories before start, never places them inside
pack code, and controls any broader filesystem access. Version changes retain
state: an incompatible state migration must be surfaced before restarting, not
silently erased. There is no migration runner or installer in this slice.

Python fixture startup emits a `start` diagnostic on stderr; EOF completion emits
`stop` and exits zero. Malformed input emits `error` and exits nonzero. These
diagnostics demonstrate lifecycle outcomes, not a readiness/acknowledgement
protocol. The host must bound startup/stop, close input on normal stop, terminate
unresponsive work after a deadline, and expose exit/error results. Automatic
restarts and replay are not defined here. Browser start errors reject; stop
removes its marker and is repeatable. Production cancellation and late replies
remain bridge/page-host integration work.

## Requests, grants and transport

`check_activation()` compares the manifest's requests with an independently
supplied origin/capability policy and exact dependency inventory. Missing grants
or incompatible dependencies fail before code loads. It does not manufacture a
policy from the manifest, prompt for permission, or enforce a sandbox. Trusted
Python code can access more than its declarations; the future host must enforce
the relevant routing/record/bridge boundaries. Validation does not prevent a
file being replaced afterward: installation must provide immutable code or
revalidate the selected files before execution.

The page fixture asks its provided bridge for `{op:"echo", text:"hello"}` and the
handler returns `{ok:true, text:"local:hello"}`. This is an **example pack payload**,
not the generic Hub wire protocol. No address, port, token, cookies or site
authorization are supplied by the manifest. The eventual bridge must bind to
loopback, authenticate connections, check origins and remove site credentials
before local forwarding. A manifest match is not proof of those protections.

Likewise, `fixtures/packs/records.jsonl` uses today's capture examples (`url`,
`status`, `body`, streaming metadata). Those original examples remain unversioned compatibility fixtures.
[capture record v1](capture-records.md) is defined separately from pack API 1.
The reader accepts unversioned/v1 inputs, rejects future versions, and ignores
other origins and missing bodies. A host must validate records before delivery;
reader scheduling and acknowledgement remain #9 work.

## Reproduce fixture evidence

```sh
python3 -m unittest discover -s tests -p test_packs.py -v
python3 tools/check_pack_fixtures.py
python3 tools/check_pack_fixtures.py --bun "$(command -v bun)"
```

Bun is used **only as the JavaScript test executor**; the page module uses browser
JavaScript. Python reader, handler and mutator checks always run, including when
Bun is unavailable. The standalone command without `--bun` reports the page test
as untested; supplying an explicit Bun path adds that test. Only the page unittest
is marked skipped if Bun is unavailable. The verifier
uses a temporary profile, validates both packs first, runs the reader, calls the
mutator on synthetic responses and executes page → bridge fixture → actual
Python handler → page. It checks configuration/output isolation, EOF shutdown,
invalid-input errors, denied origins, streaming passthrough, repeated injection,
page cleanup and bad replies. It starts no listener, service or system proxy and
does not read a production capture journal. The transport is a test subprocess
call, **not a WebSocket**. The reserved `/__tap/fixture/page.js` URL has no installed
route; its exported page functions are invoked by the fixture harness explicitly.

The validator tests also cover incompatible versions, typo/duplicate JSON fields,
missing/undeclared/escaping files, exact origins, typed overrides, independent
access grants, fixed dependencies and rejection before code execution.

An external author can copy either fixture directory, change its ID and code,
and validate it using the same command without a vendor account. The fixture
transport is still not installed execution. Authenticated live WS, installed
reader/handler/mutator bindings and the combined independently distributed
example remain #10/#11/#14 integration work. Issue #5 must be assessed against
that deliberately limited fixture scope; these additions do not complete #14 or
the release acceptance scenario.

The first immutable artifact store and actual `browser-scripts-v1` profile
binding are described in [the external pack lifecycle](pack-lifecycle.md). Other
roles still fail activation until their concrete installed bindings exist.
