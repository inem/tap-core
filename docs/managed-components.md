# Profile-owned Hub, readers and handlers

This development slice implements the joint #11 / #32 scenario on top of the
merged capture, reader and profile-bridge work (#28 / #30 / #31), including the
routing adapter extraction (#39). `install` / `on` start the components; the test
harness no longer schedules readers or handles requests. A page asks a declared
handler for the actual saved reader projection. No private TAP checkout is used.

This began as a **development-checkout** binding. The one-line installer (#49)
now downloads pinned Python/Bun/mitmproxy and writes the same bridge/components
shape under `~/.tap-core/managed/` with absolute paths inside the install root.
Pack installation remains #14. The first installed host bindings project enabled
pack `reader` / `handler` / `page` entrypoints onto the existing managed
components and bridge composition (no second store). CA trust, update and
clean-Mac matrix remain #7 / #15.

## Run and configure

Keep the checkout in a development directory such as `~/Code/tap-core`. macOS
background services may be denied access to code under protected Documents or
Desktop directories. The profile refers to this checkout; it does not copy a
runtime snapshot or act as an installer. Keep the declared code paths available
until stopping the profile.

The existing profile-bridge JSON selects the Hub port, exact allowed/excluded
origins and absolute page-script paths. Add `--components-config components.json`
to the existing `install` invocation. An illustrative binding is:

```json
{
  "version": 1,
  "python": "/absolute/path/to/python3",
  "bun": "/absolute/path/to/bun",
  "readers": {
    "projection": {
      "version": 1,
      "revision": "example-1",
      "command": ["/absolute/path/to/python3", "/absolute/path/to/reader.py"],
      "config": {"url": "http://127.0.0.1:18080/record"}
    }
  },
  "handlers": {
    "projection": {
      "command": ["/absolute/path/to/python3", "/absolute/path/to/handler.py"],
      "config": {"projection": "/absolute/profile/data/readers/projection/result.json"},
      "origins": ["http://127.0.0.1:18080"]
    }
  }
}
```

These paths are illustrative and must be supplied explicitly. There are no
production directories, account credentials, flows, adapters or journal defaults.
This slice checks Bun **1.3.11** and Python >= 3.9 before component startup
whenever the browser/control bridge is enabled. The Hub is that plane's required
WS and development channel even when no handler is installed. A reader-only
profile keeps `bridge.enabled=false` and starts without Bun/Hub.
Handler `TAP_PACK_CONTEXT` includes `profile_root` so an installed pack can
resolve sibling reader outputs without absolute config paths. This is a local
filesystem location for trusted same-user handlers, not a new grant or a
filesystem sandbox; it must not be forwarded to the requesting page.
The backend retains its existing mitmproxy 12.2.3 check. Node is a test dependency
for Playwright, not a product runtime dependency.

```sh
python3 -B tap --profile /absolute/profile install \
  --backend /absolute/path/to/mitmdump --port 18081 --routing explicit \
  --probe-url http://127.0.0.1:18080/record \
  --bridge-config /absolute/bridge.json --components-config /absolute/components.json
python3 -B tap --profile /absolute/profile on
python3 -B tap --profile /absolute/profile doctor
python3 -B tap --profile /absolute/profile where
python3 -B tap --profile /absolute/profile off
python3 -B tap --profile /absolute/profile components configure --config /absolute/updated-components.json
```

Configuration changes require `off` and apply at the next `on`. JSON `null`
disables managed components; it preserves captured data and reader checkpoints.
`bridge configure` also refuses changes while either owned service is loaded.
Changing a reader definition still requires a new reader name or explicit replay
under the existing #9 contract; configuring a binding does not reset progress.
Profiles without `components` keep their previous externally managed Hub behavior.

`status` exposes component PID, observation age/configuration match, Hub PID,
and reader phases/progress/errors. Its control-plane health covers the owned
controller and required Hub; reader outcomes are separate workload observations.
`doctor` checks Hub health
through its private endpoint as well as the proxy, capture and routing. `where`
includes both launch agent paths and component/handler logs. An absent, stale,
failed or hung Hub cannot become healthy just because injection is loaded.

## Ownership and execution

The existing proxy job is unchanged in responsibility. One additional launchd job,
`<profile-label>.components`, runs `service.py` from this checkout. It starts the
Bun Hub and schedules finite `Reader.run` batches over existing #9 checkpoints.
There is one scheduling thread per declared reader, at most eight readers.
No second cursor store, capture bus or workflow scheduler is introduced.

A controller and its required Hub must become live before infrastructure startup
reports ready. Startup readiness is bounded to 15 seconds. Reader health is a
separate workload aggregate: a reader may report backoff or failure while capture
and the owned Hub continue serving current traffic. `status` retains each failed
workload, while `doctor` continues to report the usable control plane as healthy;
startup does not call the reader healthy or alter its checkpoint.

Readers consume an initial one-record batch and subsequently up to 50 records per
batch, with a 10-second bound per worker,
then check for new input every 250 ms. A failed batch is retried after 0.5 and
1 second; after three consecutive failures that reader is parked. A cursor whose
capture segment has left retention is handled before that retry policy: the lost
boundary is recorded and the reader continues at the earliest retained record.
`off/on` is not required. Other persistent failures still exhaust the bounded
attempts.
A successful batch resets its failure counter. External effects may already have
happened before failure: cursor retention permits retry, it does not undo effects.

The controller has a profile lock. Each Hub, reader and handler invocation has a
new process group and a small guardian that watches its parent. The guardian
retains the reader lock FD and the worker inherits it. Killing the controller
therefore cannot silently unlock an ongoing reader invocation and start a parallel
retry. Normal cancellation and parent death terminate owned groups. This is a
trusted-process contract: commands must not daemonize or escape their process
group; it is not a malicious-code containment sandbox.

launchd restarts an abruptly killed controller with a three-second throttle.
Three starts within 60 seconds exhaust the budget; the fourth startup records a
failure and exits successfully to stop launchd retries. Explicit `off/on` resets
the budget. A dead/hung Hub or invalid persisted state stops the controller with
an observable error; manual recovery is required. Handler failures affect their
request and do not restart the Hub. There is no unlimited automatic replay.

`off` restores routing first, then removes the component job and stops its owned
processes, then stops the proxy. Failed component startup also unwinds the proxy
started for that attempt. Exact job labels and owned process groups are used;
no process-name or port-wide kill is used. A preexisting foreign listener is
refused and left running. An incomplete cleanup is an error, not successful off.

## Page and handler protocol

This is an explicit new `tap.bridge/v1` interface, not compatibility with legacy
`TapProbe`, Actions, Needs, eval, handles or adapter protocols. Opt-in managed
profiles serve `page-runtime.js` and `tap.page-plan/v1` directly through their
existing reserved proxy routes. The runtime polls the plan every two seconds
(five after an unavailable or malformed response). A changed origin-scoped
revision reloads classic scripts with a `tap-ui` cache-buster. Plan polling stays
on the HTTP path and does not depend on a WS message. The enabled browser/control
plane also runs the Hub: `Welcome` establishes its page session and `PlanChanged`
wakes the same HTTP reconciliation immediately; neither message carries executable
code or new authority.
`window.TapBridge.isReady()` reports a welcomed connection;
`await TapBridge.request('projection', args)` returns the handler value or rejects
with an error carrying `code` and `completion: "unknown"`.

| Message | Fields |
|---|---|
| Page → Hub Hello | `version`, `kind: "Hello"`, document UUID `page`, exact `origin` |
| Hub → page Welcome | `version`, `kind: "Welcome"`, same `page`, fresh server UUID `session`, current global plan `revision` |
| Hub → page PlanChanged | `version`, `kind: "PlanChanged"`, current `session`, new global plan `revision`; page rechecks its origin plan over HTTP |
| Page → Hub Request | `version`, `kind: "Request"`, current `session`, request UUID `id`, `handler`, JSON `args` |
| Hub → page Result | `version`, `kind: "Result"`, same `session` and `id`, `ok`, and `value` or typed `error` |

The session belongs to a socket, even when a document reconnects using its same
page UUID. Results are sent only through that socket. Closing it cancels its
owned handlers; a late result is discarded. The page rejects pending requests
on disconnect and never automatically resubmits them. It makes at most eight
consecutive connection retries with 200 ms–5 s backoff. Each new welcome resets
the connection retry budget. Reload reconnects after the budget is exhausted.

A declared handler receives one UTF-8 JSON line on stdin:

```json
{"version":1,"request_id":"request UUID","args":{"example":"input"}}
```

Its `TAP_PACK_CONTEXT` JSON contains `version: 1`, handler/origin/page/session/
request IDs, the binding's `config`, and profile-owned `state_dir`, `output_dir`,
`log_dir`. It returns JSON on stdout and exits 0:

```json
{"ok":true,"value":{"example":"result"}}
```

A controlled failure returns `{"ok":false,"error":{"code":"not_ready","message":"No projection yet"}}`.
Hub validates and reconstructs the envelope; a handler cannot override transport
session/request correlation fields. Diagnostics belong on stderr. The most recent
nonzero-exit diagnostic is retained in the handler's private `last-failure.json`.

Bounds: 64 sockets, a three-second Hello deadline, 64 KiB WS messages, four
concurrent requests per socket, eight active handlers globally, 1,024 request IDs
per session, five seconds per handler, 256 KiB stdout and 64 KiB stderr per handler.
The page's response deadline is 6.5 seconds. Oversized output, missing commands,
nonzero exits, malformed JSON/results and protocol mismatch fail explicitly;
none is converted to a successful empty result. Protocol violations close the
connection; typed handler errors remain request-local.

## Authority

The existing proxy route checks the requested original origin, page token and
WS Origin, removes site credentials and caller-supplied forwarding authority,
and forwards only an authorized reserved request. With managed components it
adds a second private token from `state/component-token` (0600).

The Hub binds to 127.0.0.1 and independently requires that private component token,
the page token, an allowed/non-excluded original origin, and matching WS Origin.
Knowing the injected page token and forging forwarding headers is insufficient
to call the direct listener. A handler additionally needs a grant for that origin.
`/health` requires the private component token as a bearer credential.
Revocation is a stopped-profile configuration change followed by `on`: existing
connections are closed and the new policy is loaded at both boundaries.

This establishes a page-to-local boundary, not pack isolation. Code running as
the same user can read profile credentials; trusted commands inherit the service
environment and normal user filesystem/network access. All page scripts on an
allowed origin share page authority. The origin grant is not a separate identity
for each script, tab or pack. No site/account credentials are required by this
example, and no claim of a same-user sandbox is made.

## State, provenance and recovery handoff

The concrete legacy cut is recorded in [the live-slice map](live-slice.md#decision-stream-node-composition-is-deferred).
This implementation retains its demonstrated transport responsibilities: page
identity, Hello validation, request/result correlation and same-origin WS routing.
The new `hub.mjs`, `page-runtime.js`, `components.py`, `service.py`, `guardian.py`
and managed fixtures are newly authored in this repository under its MIT license;
no private source file or third-party implementation was copied into this change.
The existing capture/bridge/reader files retain their recorded source history.
Python stdlib, browser APIs and the declared Bun runtime are used directly.
Runtime redistribution/license notices still belong to the delivery acceptance.

The large legacy Hub mixes `acceptEvent`, `appendJournalEvent` and `rebuildState`
with Actions/Needs/Flows. None of that event state is implicitly loaded here.
The new wire version makes this separation explicit; old clients keep using
externally configured legacy Hub profiles until a deliberate migration.
Named stream-node composition remains **deferred**, not rejected. Future semantic
coordination can use this transport without making its journal the core protocol.

Recovery input for #12:

| Point | Durable acknowledgement / uncertainty |
|---|---|
| Capture appended | Existing capture/journal contract; drops/errors stay visible |
| Reader invocation begins | Existing stable delivery/invocation IDs and inflight checkpoint |
| Worker exits successfully | Runner advances checkpoint; crash before that can replay an already produced effect |
| Hub receives Request | Volatile session-local ID tracking only; no durable receipt |
| Handler exits with result | Result exists in Hub memory; no durable outbox |
| Hub sends Result | WS send is not a durable page acknowledgement |
| Page resolves Promise | No acknowledgement is persisted or sent back to Hub |

There is no exactly-once or crash replay guarantee for page requests. A disconnect,
timeout or crash can leave the caller uncertain even if an external effect occurred.
Handlers with effects must supply their own idempotency/reconciliation policy.
The example only reads a projection; its page owns retrying the `not_ready` query.

## Evidence and limits

`TAP_TEST_BUN=/absolute/bun python3 -B -m unittest discover -s tests` includes a
real Bun listener/subprocess test with 24 protocol/access/failure assertions,
reader lock/guardian crash checks and simulated lifecycle failures. Without Bun,
that integration test is explicitly skipped; it must run for this slice's review.
On 2026-09-07 the full suite passed **198 tests**, including the Bun integration
and token-file diagnostic regressions (`7a23d65`). The full launchd/browser live
run was then repeated at `b117825`, which contains those fixes; the report records
that source commit. Its exhausted-budget check observes the loaded job remaining
without a PID for seven seconds (more than two restart throttle intervals), with
no additional start recorded. Only documentation/report changes follow that run.

`tools/check_managed_slice.py` takes explicit backend/Bun/Node/Playwright/Chrome
paths and `--output report.json`. It creates temporary profiles and synthetic
loopback origins, supplies records, observes browser results and injects process
faults. Only normal CLI lifecycle commands start/schedule product components.
The checked-in [live report](managed-live-2026-09-07.json) records versions and:

- automatic retained backlog and fresh capture → projection → page, two tabs and
  origins, excluded origin and handler grant, foreign-base token protection;
- repeat `on`, `off/on` checkpoint resume, controller crash/restart and restart
  budget exhaustion, dead/hung Hub, and successful manual recovery;
- occupied Hub port preserving its owner and cleaning the proxy, missing Bun,
  real failing reader workers with bounded retries and no checkpoint advancement;
- temporary job cleanup and unchanged system proxy configuration.

The fixture browser is a fresh headless context. Its background browser requests
may also reach the temporary proxy; there is no user account/session, system CA
installation or production capture. The asserted application cycle uses controlled
loopback HTTP/WS. This is not TLS/site/browser breadth acceptance, native capture,
clean-Mac delivery, installed pack, update/recovery acceptance or CI infrastructure.
