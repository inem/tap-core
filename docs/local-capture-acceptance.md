# Local Capture: executable acceptance and OS approval boundary

Related to #3, based on main `a7c043f` and the earlier #17 attribution report.
Local Capture is the first backend candidate for app routing. A custom process
helper is not justified by the existing evidence. This PR does not close #3.

## Observed on 2026-09-07

macOS 15.6.1 arm64; coordinator Python 3.9.6; standalone mitmproxy 12.2.3 with
embedded Python 3.14.4. The controlled clients send only synthetic HTTP to a
private loopback origin, using raw HTTPConnection rather than system/environment
proxy configuration.

- **Explicit control passed:** client A, explicitly connected to the fixture
  proxy, received the marker; client B, connected directly, received the origin
  response. After backend shutdown both clients received the direct response.
- **Local Capture is blocked at OS approval:** starting `local:<own-client-PID>`
  caused the bundled backend to install `/Applications/Mitmproxy Redirector.app`
  and register `org.mitmproxy.macos-redirector.network-extension` (2.0/1).
  The extension reports `[activated waiting for user]`. No Local Capture routing
  success, selector update or native addon attribution was observed.
- The installed app's deep/strict codesign verification passed outside the agent
  sandbox: Developer ID Application Maximilian Hils, team `S8XHQB96PW`.
  This signature check is not clean-machine installation acceptance.
- Settings → General → Login Items & Extensions → Network Extensions visibly
  lists **Mitmproxy Redirector.app / network-extension**, switched off. Turning it
  on grants the shared OS network extension access; it is a separate user step.
- The harness's backend/clients stopped, its temporary profile was removed and
  effective proxy settings matched before/after. The installed Redirector app and
  pending extension registration remain. Process cleanup is **not uninstall**.

The first absence check in the sandbox failed; an unsandboxed successful
`systemextensionsctl list` established absence before the first activation.
Likewise, signature verification was repeated successfully outside the sandbox.
An inspection failure is not known absence or a bad signature.

Final source hashes and bounded rerun results:

- [Explicit control](results/local-capture-control-2026-09-07.json).
- [Local startup awaiting approval](results/local-capture-attempt-2026-09-07.json).

The final local report begins with the extension already pending, because the
first invocation installed it. It does not claim to reproduce a clean install.
Raw backend logs are optional private output, not public evidence.

## Repeat the same scenario

No third-party Python packages are needed by the coordinator. The supplied
backend must support its addon APIs. These commands do not run from the ordinary
unit suite or CI:

```sh
python3 -B tools/check_local_capture.py --backend /path/to/mitmdump \
  --mode explicit-control --output /tmp/tap-explicit-control.json

# Explicit OS setup/activation acceptance: can install/launch the shared
# Redirector and request macOS approval. Only own gated client PIDs are selected.
python3 -B tools/check_local_capture.py --backend /path/to/mitmdump \
  --mode local --output /tmp/tap-local-capture.json
```

Run the second command after the user has approved the extension on the prepared
test machine. Do not run against another active Local Capture controller: the
backend's shared extension is not a proven isolated per-profile resource.
A prepared extension and a controlled test host are prerequisites for the full
routing experiment, not implicit properties of a temporary profile.

Exit 0 means the cases for the **named mode** passed. Explicit-control success is
not Local Capture evidence. Exit 2 reports blocked startup, failed routing,
harness failure or cleanup failure; inspect the JSON, not just backend exit code.
In the observed blocked run the backend itself exits 0 on requested shutdown.

Local mode requires A intercepted/B direct. Only after that result does the
addon request a swap to B; the harness then requires A direct/B intercepted on
new connections. A `running` hook or accepted configuration alone cannot pass
the test. Both clients remain alive while the PID selectors are used. The file
controller accepts only those two predeclared own PIDs, never broad names or an
empty all-process selector. It is a fixture, not a proposed production controller.

The explicit listener is reserved until backend start; there remains a bounded
close-to-bind race because mitmdump does not inherit the reserved socket. Failure
to start or return the unique fixture response fails the run.

## Limits and remaining acceptance

A successful future loopback run would establish only PID selection and a rule
swap for new IPv4 HTTP connections. If loopback is unsupported, repeat against a
controlled non-loopback origin with explicit fixture guards; do not substitute
production websites, an account or broad capture.

Still needed in #3: restart/PID lifetime, source-port reuse through this backend,
parent/helper and stable application identity, name-selector collisions,
unknown-source behavior, attribution available at the addon boundary, and the
accepted first-release scope. The existing #17 lsof experiment does not establish
these for Local Capture. Performance measurements must describe the actual
selected workload; none are claimed from a startup timeout.

For #6/#7: exclusion from Local Capture does not imply a direct route when the
client also uses explicit/system proxy. Multiple backend modes do not themselves
prove a combined host × transport × app policy. UA is not process identity;
synthetic Local Capture endpoints are not a ready lsof port-lookup fallback.
Process selection, TLS handling and record retention remain separate decisions.

HTTPS/CA/injection, UDP/IPv6, existing connections during rule changes, two Local
Capture profiles and other network-filter interoperability are untested. No
other network filter was disabled. The machine's existing TAP was not migrated.

## Validation of the evidence tool

127 unit tests passed locally. Four added checks cover unavailable extension
inspection, a partial startup event, backend failure followed by successful
cleanup, and a fake running backend with two direct responses. The latter must
fail routing, not produce a false green result. These tests use a fake backend
and do not install or activate an extension.
