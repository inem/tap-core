# Source application attribution and routing: first evidence

Issue #3; measured 2026-09-07 on macOS 15.6.1 / arm64. This is an
investigation result and a proposed v0.1 scope, not a completed application
routing feature. The running TAP and system proxy were not changed.

The existing TAP lifecycle (`install`, `doctor`, `status`, `where`, `on`, `off`)
and `networksetup` routing across configured services with user bypass rules are
the product's starting mechanism. Local Capture is an investigated addition,
not a reason to replace that lifecycle or discard its failure recovery behavior.

## Evidence boundary

| Question | Result | Evidence |
|---|---|---|
| Two processes contacting one origin can be distinguished | **Verified**, own-user IPv4 loopback TCP | Python and curl: 10/10 samples matched both connection tuples to the actual child PIDs; executable basenames resolved via `proc_pidpath` |
| Restart | **Verified** for the controlled curl process | New PID/new connection, same executable basename; old connection disappears |
| Reused source port | **Verified** for the controlled Python process | Same TCP 4-tuple was assigned to a different PID; fresh lookup found the new owner |
| Parent versus child socket ownership | **Verified** | Looking up only the harness PID does not return child-owned connections |
| Closed/short-lived connection | **Unknown** after exit | A later query has no owner; absence is not a policy decision |
| PID reuse | **Unresolved** | Deliberately not forced by spawning huge process counts |
| macOS application bundle identity/helper grouping | **Unresolved** | Executable basename is not application identity; no browser bundle acceptance yet |
| Transparent intercept/passthrough by application | **Unresolved** | Local Capture not activated on this machine |
| Explicit-client proxying | Outside this experiment | Does not prove per-application transparent routing |
| UDP, IPv6, VM/container origin, intermediary proxies | **Unresolved** | Not exercised by the TCP fixture |

The fixture invokes `lsof` with only its own child PIDs and matches both endpoints,
then resolves the executable for those PIDs. It is not a general traffic monitor:
the set of candidate processes is supplied by the fixture. No unrelated process
list, personal traffic, source paths, or private source code is published.

Measured `lsof` wall time for two PIDs was **82.02–85.18 ms**, median **82.68 ms**
over ten queries. This includes process launch and inspection; it is not proxy
latency, CPU cost, or a system-wide scaling measurement. This implementation must
not be put synchronously into capture hooks. It also demonstrates why a cached
port-to-PID map without connection lifetime handling is insufficient.

Run the actual experiment:

```sh
python3 tools/characterize_process_attribution.py
```

An environment that denies loopback binding or OS process inspection must report
an environment failure. On the inspected machine this needed execution outside
the agent sandbox, but no `sudo`, root process, or system settings change.

## Earlier monitoring found

A separate local OS-observer prototype contains a periodic process/socket
snapshot monitor and readers for process listings, network listings, and
pre-generated `lsof` dumps. The monitor tracks appearances/disappearances and new
remote connections. Its FD reader consumes dump files; it does not itself provide
live connection interception. No connection between that prototype and TAP's
proxy routing was found. The exact local paths and source revision are in the
private task handoff; its source has not been copied into this public repository.

This is a candidate for the user's remembered process monitoring, not proof that
the original TAP already implemented per-app routing. The search covered TAP's
known checkouts/CLIs and the newly located observer; it was not an exhaustive
search of every historical working tree.

## Existing backend candidate

The installed standalone mitmproxy **12.2.3** parses PID/name Local Capture modes
and exposes `LocalRedirector.set_intercept`. Its availability function returns
no platform-level error. These are **offline capability checks**, not evidence
that the required macOS component is installed or capture works.

```sh
profile=$(mktemp -d)
mitmdump --no-server --set "confdir=$profile" -q -s tools/probe_local_backend.py
```

The profile is temporary and its generated certificate is never trusted. This
probe neither starts Local Capture nor lists active applications.

Upstream documents Local Capture selection by process name/PID and outbound
capture on macOS. Its implementation uses a macOS Network Extension. See the
[mode documentation](https://docs.mitmproxy.org/stable/concepts/modes/#local-capture)
and [upstream explanation](https://www.mitmproxy.org/posts/local-capture/macos/).

At the time of the first #17 experiment, read-only checks found **no installed
Mitmproxy Redirector app or registered mitmproxy Network Extension** on this machine. Launching Local Capture would add
a shared OS component rather than just start a second isolated TCP listener.
The coordinated task explicitly excluded that activation from this pass. This
is not an automatic approval rejection and not evidence of backend failure.
Other network extensions were present; interoperability is untested and their
identities are intentionally not included in this public result.

### Source-inspected behavior to validate live

The following comes from upstream source, not execution of the installed Swift
extension. The inspected mitmproxy-rs source revision is
`58bea7f7e0b7b00c7d91b9997c83299ae3e0922e`; it is **not established as the bundled
Rust/macOS version**. Package metadata was unavailable in the standalone build.

- The extension resolves an OS-provided audit token to PID/path. Missing process
  information leaves the flow to the OS. With a PID but no path, a PID rule can
  match while a name rule cannot. It does not traverse parent processes.
  [Process lookup](https://github.com/mitmproxy/mitmproxy_rs/blob/58bea7f7e0b7b00c7d91b9997c83299ae3e0922e/mitmproxy-macos/redirector/network-extension/ProcessInfoCache.swift)
  and [flow decision](https://github.com/mitmproxy/mitmproxy_rs/blob/58bea7f7e0b7b00c7d91b9997c83299ae3e0922e/mitmproxy-macos/redirector/network-extension/TransparentProxyProvider.swift).
- A name selector is a **substring of executable path**, not a bundle ID or
  exact display name. Ordered include/exclude operations can re-include an
  earlier exclusion. Raw backend selectors must not silently become the product
  policy language. [Selector implementation](https://github.com/mitmproxy/mitmproxy_rs/blob/58bea7f7e0b7b00c7d91b9997c83299ae3e0922e/mitmproxy-macos/redirector/network-extension/InterceptConf.swift).
- Rule updates are sent over the control channel and evaluated for **new flows**.
  An existing TCP connection is not demonstrably re-routed, nor is there a
  documented end-to-end policy-applied acknowledgement in the inspected path.
  [Control channel](https://github.com/mitmproxy/mitmproxy_rs/blob/58bea7f7e0b7b00c7d91b9997c83299ae3e0922e/src/packet_sources/macos.rs).
- The mitmproxy 12.2.3 server permits one LocalRedirectorInstance per process;
  stopping it clears its intercept spec while retaining the redirector. This
  does not establish support for simultaneous independent local-mode profiles.
  [Mode lifecycle](https://github.com/mitmproxy/mitmproxy/blob/v12.2.3/mitmproxy/proxy/mode_servers.py).

### Attribution can disappear at the Python boundary

The installed Rust Stream interface declares `pid` and `process_name` extra
information. But the installed `Client` dataclass has neither field. The 12.2.3
[connection handler](https://github.com/mitmproxy/mitmproxy/blob/v12.2.3/mitmproxy/proxy/server.py)
copies transport and endpoints, not those application fields, into `Client`.
This is a concrete extraction concern for #6: backend selection may work while
normal capture addons still lack recorded application provenance. Do not claim
`flow.client_conn` already gives the originating application. An adapter or
upstream change may be necessary; that is not yet a reason for a custom OS helper.

For macOS TCP, the inspected Rust path synthesizes a source endpoint rather than
exposing the original client port. Therefore the loopback `lsof` experiment is
not a proposed fallback for Local Capture's synthetic endpoint.

## Proposed app-scope for v0.1 and inputs to #6

1. Validate the first portable runtime (#4) through an explicit proxy in its own
   profile while preserving the existing lifecycle and bypass behavior.
   It may record an **unknown** source application. No transparent app-scoping
   promise follows from a client deliberately using that proxy.
2. Evaluate macOS Local Capture as the **first candidate for release app routing**.
   Its support remains experimental until the acceptance below passes. Do not build a new native routing helper before
   establishing what the existing backend cannot deliver.
3. Keep user exclusions authoritative over pack requests. Translate policy into
   backend selectors centrally; do not concatenate arbitrary pack spec strings.
   Validate ambiguous paths/selectors and account for helpers explicitly.
4. Never silently broaden an app-limited request to all traffic when attribution
   or backend setup fails. For an explicit proxy, report unknown application;
   for optional app capture, report unavailable/unverified and leave unselected
   traffic to its existing route. This is an observability policy, not a firewall.
5. Distinguish source identity, selected capture route, TLS decoding, and record
   retention. “Not saved” is not “not intercepted”; TLS passthrough through a
   backend is not a direct OS route. `host × transport × app` spans these stages.
6. PID rules are temporary process-instance selections. Stable application
   rules need executable/bundle identity and tested helper/restart behavior.
   Never persist a naked PID as durable application identity.

This was the original research recommendation. The accepted release decision and
explicit handoff to #6/#7 are recorded in [the follow-up](local-capture-acceptance.md#accepted-research-outcome-and-remaining-owners).

## Separate clean-Mac and Local Capture acceptance

Use a clean validation Mac/VM capable of the extension, or a deliberately prepared
test host. Installation/activation of Redirector must be part of an explicit OS
installation acceptance, not a hidden side effect of starting a dev profile.
Check the signed/notarized component, user approval steps, refusal/cancel,
update and removal; upstream's [macOS package notes](https://github.com/mitmproxy/mitmproxy_rs/blob/58bea7f7e0b7b00c7d91b9997c83299ae3e0922e/mitmproxy-macos/README.md)
describe the separate signed component. Do not trust a test CA globally for the
plain HTTP routing experiment below.

Minimal first routing experiment, after that setup:

1. Serve a file named `tap-core-routing-fixture` containing `tap-core-direct` from
   a new temporary directory with `python3 -m http.server 18081 --bind 127.0.0.1`.
   Confirm port 18081 is free or choose another test port.
2. In two separate terminals start the following gated client. Record the PID
   printed by each as A and B. Press Enter only when directed. The shell is
   replaced by curl, preserving the selected PID. No system proxy is consulted.

   ```sh
   sh -c 'echo "fixture PID: $$"; read trigger; exec /usr/bin/curl --noproxy "*" --max-time 10 --silent --show-error http://127.0.0.1:18081/tap-core-routing-fixture'
   ```

3. In another terminal start **only PID A**: `mitmdump --mode local:<A> --set
   confdir=<new-test-profile> --set connection_strategy=lazy -q -s
   tools/local_capture_marker.py`. Replace the placeholders with fixture values;
   never use bare `local`, a broad `Python` name, or production profiles.
4. Release A and B. Success requires **A returns `tap-core-intercepted` and B
   returns `tap-core-direct`**. A log entry alone is insufficient. Both failures,
   or two direct responses, must be recorded as unresolved/unsupported for this
   setup, not a pass. Loopback may have backend-specific limitations; if it does,
   repeat against a controlled non-loopback origin on the test network and adapt
   the marker's exact host guard. Do not substitute a personal site/session.
5. Stop the fixture backend and clients. Verify a fresh client gets the direct
   response. Check shared network settings/extensions separately from profile
   cleanup; stopping a process does not prove uninstall.

The marker addon was validated with an isolated explicit proxy; **the Local
Capture procedure itself has not run**. Required follow-up cases: swap A/B rules
without restarting the controller; keep a connection open across that change;
restart selected apps; select a parent with a connecting child; path/name match
collisions; unknown audit token; TCP/UDP and IPv4/IPv6; direct versus chained
proxy; browsers' network helpers; coexistence with network filters/VPNs; two
profiles, backend crash and reconnect. Measure request latency/CPU/memory and
policy propagation separately from the `lsof` timing above.

Machine-readable measurements are in
[`process-routing-2026-09-07.json`](results/process-routing-2026-09-07.json).

## Follow-up: actual activation attempt

The [executable acceptance follow-up](local-capture-acceptance.md) replaces the
manual-only procedure with a controlled harness. Its explicit-proxy control
passed; Local Capture installed the bundled Redirector and reached macOS
`activated waiting for user`. The OS approval step and routing acceptance remain
open. This supersedes the earlier statement that Local Capture was never started,
without turning the startup attempt into routing evidence.
