# One live vertical slice before completing M1

Issue #29 supplies an early integration check. A controlled HTTP response is
captured by the new checkout runtime, processed by the #28 reader runner, and
returned from its saved projection through the legacy Hub and injected page
runtime to the requesting browser tab. It is one connected data path, not an
HTTP check followed by an unrelated WebSocket echo.

## Observed result, 2026-09-07

The [machine-readable report](live-slice-2026-09-07.json) records the source
hashes, runtime versions and assertions. The run used macOS 15.6.1 arm64,
mitmproxy 12.2.3, Python 3.9.6, Bun 1.3.11, Chrome 152.0.7977.76 and
Playwright 1.62.1. This is one tested configuration, not a support matrix.

1. The harness creates two loopback HTTP origins, a fresh profile, an empty
   Hub journal/mailbox/flows/adapters directory, and private fixture credentials.
2. The existing `install`, `on` and `doctor` commands start and verify a uniquely
   named launchd profile in explicit routing mode. Only the test browser uses
   its proxy; no system network settings or CA trust are changed.
3. Unmodified legacy `mutators/site-probe.py` injects the legacy runtime into
   the allowed page and rewrites its same-origin runtime/WS route to the Hub.
   An explicit test addon also injects the small example action/button behavior.
   The original HTML contains controls but no TAP implementation.
4. A real headless Chrome tab requests `/record`. Its value is changed after
   startup probes, so a probe record cannot satisfy the final assertion. The
   page consumes and discards the response body, then emits a WS request event.
5. The local harness observes that request in the Hub journal, waits for the
   capture writer, and runs `reader run projection`. The reader filters the
   exact fixture URL and writes `{record_id, value}` to its own output file.
6. The harness reads that file and sends `fixture.render` through the existing
   authenticated controller endpoint to the requesting page ID. The page
   receives a WS Command, displays the result, and returns a WS Result. Capture,
   output file, browser text and the controller acknowledgement must agree.
7. A second connected tab must remain unchanged. A page on the denied origin
   must have no bootstrap. Finally the browser, Hub, temporary launchd job and
   profile are removed; system proxy settings must match the initial snapshot.

The page does not receive the expected value from browser automation. Automation
clicks the button and reads rendered text; the value travels through the saved
reader output and actual WebSocket connection. No runtime eval/arm capability is
used. Recorded frame kinds include Hello, Welcome, Event, EventAck, Command and
Result. Record counts can vary with incidental browser HTTP requests.

## Reproduce with explicit development inputs

This is a trusted-source integration harness, not a packaged feature. Supply a
local legacy source snapshot matching the report hashes. It reads the four
listed source files in place and does not copy their implementation into this
public repository. The new runtime/reader input is PR #28, `8b37be5`, based on
main after #27, `a7c043f`. The #28 runner was still on review for this experiment.

```sh
python3 -B tools/check_live_slice.py \
  --source /absolute/path/to/trusted-legacy-tap \
  --backend /absolute/path/to/mitmdump \
  --bun /absolute/path/to/bun \
  --node /absolute/path/to/node \
  --playwright /absolute/path/to/node_modules/playwright \
  --chrome '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' \
  --output docs/live-slice-2026-09-07.json
```

The command needs access to the current user's launchd domain and permission to
start local listeners and a separate headless browser. Node/Playwright/Chrome
are explicit test tooling, not additional mandatory core runtimes. It neither
uses the user's normal browser profile nor runs old `tap on/off`. Child output
and fixture credentials stay in the private temporary directory and are not
included in the public report. Nothing from production capture or the production
Hub journal is used. CI remains deferred.

The harness holds two distinct loopback listener reservations while preparing
the fixture, then releases each immediately before starting its service (Hub
spawn or proxy installation). Context cleanup closes any reservation still held
after a failure. The services open their own sockets; they do not inherit these
listeners, so another process can still claim a released port during the brief
startup handoff. Startup checks detect failure; this is not atomic port ownership.

## Integration gaps exposed by this wiring

| Concrete coupling or missing product behavior | Follow-up |
| --- | --- |
| Profile installation accepts addon paths but does not configure or supervise a Hub/reader pack. The harness still starts the Hub and calls the reader. | #4 / #9 / #14: installed host and lifecycle integration; do not reopen the completed checkout-runtime scope of #4 merely for this extension. |
| The legacy injector expects separate token/allowlist files and a fixed Hub host/port. A temporary wrapper binds those to this profile. | #6 / #10 / #11: explicit shared configuration and routing ownership. |
| `createHub` loads flows, adapters and recovered needs at startup. Empty paths must be supplied through its programmatic API; the CLI does not expose all of them. | #11: separate the generic page bridge from the coordinator and remove implicit application startup. |
| A page request reaches the local handler through polling the existing Hub journal. The handler is fixture orchestration in this harness, not a new installed extension API. | #11 / #14: select a concrete supported handler boundary from this scenario. |
| The capture stream also contains browser/runtime resource traffic; the reader must filter its own URL. The runner starts one process per retained record. | #6 / #9: transport policy, backlog and invocation cost; this run supplies no throughput benchmark. |
| A finite healthy round-trip supplies no evidence about uncertain effects, failed-event cursor advancement or recovery of a persisted action. | #12: dedicated fault/reconnect matrix still required. |
| Observations and assertions are available only in the harness report, not combined product diagnostics. | #13: expose the states needed to locate a broken link. |
| A sibling source checkout and explicit test tools are still needed. | #2 / #7 / #14 / #15: provenance, independent artifact, installation/update/removal and clean-Mac acceptance. |

## Transport and evidence boundaries

| Surface | Evidence here |
| --- | --- |
| Controlled HTTP response body → capture → reader → file | Verified live. |
| Injected runtime and same-origin page ↔ Hub WS via proxy | Verified live, including the actual projection result and targeted tab. |
| Second tab / denied origin | Non-target tab unchanged; denied origin receives no bootstrap. Only one origin was allowed. |
| HTTPS, CA installation, CSP/nonce matrix, browser restart | Not exercised. |
| SSE passthrough or capture of arbitrary third-party WS frames | Not exercised by this slice. Own bridge frames are not evidence of generic WS capture. |
| ChatGPT/conduit, real account data, organizer or external API actions | Not exercised or required by this example. |
| Installed external pack, crash recovery, clean-Mac acceptance | Not established. |

This result does not close the horizontal M1 issues or replace #7/#15. It shows
that the selected mechanisms can carry one real result end to end, and identifies
which connections are currently supplied by the harness instead of the product.

## Decision: stream-node composition is deferred

Named output streams and arbitrary stream-node composition are **deferred**, not
rejected. The current record journal and independent reader progress meet the
concrete capture input needed by this slice. No demonstrated composition case
requires an additional general bus yet. Outputs remain reader-owned artifacts;
this is not a permanent restriction against adding composable streams later.

The proposed generic bridge is underneath semantic coordination. Its candidate
responsibilities are page connection/session identity, origin/auth checks,
addressing, and request/result/error correlation. Actions/Needs/Flows
interpretation, work selection and application-specific materialized sources
belong above it. The current Hub contains both; this experiment deliberately
starts it without flows and does not claim the source separation is implemented.

Before #11's extraction, account explicitly for shared event IDs, journals,
reconnect cursors and recovery ownership: these cannot simply be deleted with
the semantic functions. `validateHello`, `validateResult`, `commandPage`, page
registration and WS routing are transport candidates; `loadFlows`,
`normalizeNeedRecord`, `interpretAction`, `dispatchOpenNeeds` and
`maybeFulfillFromObservation` are coordinator candidates. `acceptEvent`,
`appendJournalEvent` and `rebuildState` require separation of shared delivery
mechanics from semantic state. This function map guides the next code change;
it does not certify those functions as safe standalone APIs.
