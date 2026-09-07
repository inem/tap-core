# Technology constraints for the first TAP Core release

Recorded: 2026-09-07. This is the shared starting point for implementation tasks.
Requirements below constrain the release. Working choices prevent independent
stack selection by each task; they can be revised with a concrete justification.
They are not evidence that a component has already been ported or verified.

## Release requirements

1. **Local operation.** Capture, processing, injection and the local bridge must
   work without a TAP cloud account, billing service or remote database. Installing
   or updating software can require downloads; accessing a target site still
   requires that site's network and account access where applicable.
2. **An installable product.** An end user must not need a source checkout,
   developer toolchain, Docker, mise or manually assembled runtimes. Homebrew can
   be a developer/convenience path; it must not be the only clean-Mac release
   acceptance path. Required runtimes must be bundled or installed explicitly by
   the product's supported setup procedure.
3. **Controlled dependencies.** Runtime/build dependencies must be declared with
   reproducible versions or lockfiles, platform requirements and license notices.
   No implicit dependency on another checkout, global package or floating
   `latest`. Do not upgrade the user's existing TAP installation during extraction.
4. **Isolated profiles.** Configuration, state, data and listening endpoints must
   be explicit. A development/test profile must coexist with the user's normal
   profile. Pack state must not live inside its installed code directory.
5. **Bounded capture.** No slow subprocess, unbounded body buffering or synchronous
   bulk storage work on the proxy hook path. Define queue/disk limits and expose
   drops/errors. Do not silently convert a streaming response to a buffered one.
6. **Open extension boundary.** A third-party pack can use the same documented
   installation and invocation interfaces as a commercial pack without a vendor
   account. A pack's additional executable dependencies are its declared delivery
   responsibility, not automatic global requirements for core.
7. **Explicit local authority.** Controller endpoints bind to loopback by default.
   Page-to-local access needs origin and authentication checks. Site credentials
   must not be forwarded onto the local bridge. Pack declarations describe access;
   they are not proof of an execution sandbox. In-process hooks are trusted code.
8. **Portable results and visible compatibility.** Published data/pack/controller
   interfaces have explicit versions and examples. Incompatible inputs must fail
   clearly. Progress and replay semantics must not depend on undocumented internal
   objects in another process.
9. **Separate tests from claims.** Use synthetic fixtures by default. Fixture
   checks, live traffic checks and clean-machine acceptance are distinct evidence.
   A successful build does not establish browser/OS support.

## Working choices

These are the defaults for the first extraction, pending evidence or a different
project decision. Their purpose is to avoid gratuitous rewrites and extra runtime
requirements, not to preserve the legacy architecture.

| Area | Starting choice | Constraint / reconsideration trigger |
|---|---|---|
| First platform | macOS; arm64 is the first validation target. | Minimum OS version and browser versions must be established by acceptance. Intel macOS, Windows and Linux are not implicitly supported. Keep OS-specific operations behind explicit boundaries. |
| Interception backend | mitmproxy; Python for its addons. | Reuse supported backend mechanisms before implementing another proxy. Keep site parsing and UI logic out of generic hooks. The backend choice does not dictate reader languages or repository layout. |
| CLI and profile extraction | Preserve `install`, `doctor`, `status`, `where`, `on` and `off` and their verified lifecycle behavior. Python is the candidate for newly separated coordination; existing shell code can remain while its behavior is characterized. | A language preference is not a reason to replace working lifecycle logic with a new launcher. Isolate paths/OS operations first; justify implementation changes by deployment, testing or platform benefit. |
| Existing local WS component | JavaScript on Bun as the initial extraction candidate. | Bun-specific APIs already exist in the source. Do not introduce a separate Node.js requirement or rewrite the bridge just to standardize syntax. Python-only consolidation remains an option if it demonstrably simplifies delivery without losing behavior. |
| Page runtime | Browser JavaScript; TypeScript is acceptable as an authoring choice if built to packaged JavaScript. | No Node/Bun APIs in the page, required browser-side build step, CDN-loaded code or site framework dependency in the generic bootstrap. Do not force a full frontend framework for an injected script. |
| Core runtime budget | The Python interception environment and, where the bridge is used, at most one additional general-purpose runtime. | Do not automatically carry Elixir, Go or another legacy runtime into the mandatory core installation. Existing readers can remain source evidence or explicitly declared pack dependencies. A necessary native OS helper is evaluated separately in #3. |
| Cross-process data | Explicit UTF-8 JSON messages/records; HTTP/WS for the existing controller/page connection, streaming records for readers. | Version the required shapes. Do not introduce gRPC, a remote broker or a general workflow language without a concrete unmet scenario. Existing YAML declarations are source material, not automatically the new public API. |
| Local storage | Files for captured bodies and an explicit local record format; an embedded store such as SQLite may be selected for indexes/checkpoints. | No external database service. Do not freeze JSONL byte offsets as the consumer contract. The #8/#9 decision must account for rotation, retention, replay and crashes. |
| Platform process routing | Evaluate mitmproxy Local Capture before writing a custom helper. | Upstream documents selection by process name/PID. Verify installed-version behavior, permissions, source attribution, reconfiguration and conflicts on macOS. Documentation is not a live acceptance result. |
| Product UI | CLI and open example-pack surfaces for the first core slice. | No mandatory Electron/Tauri/desktop shell in this milestone. A later UI must not be required to keep capture and data access working. |

## Runtime decision for v0.1 (2026-09-07)

The mandatory core installation and the open example pack do **not** require
Elixir/OTP. The independent finite reader runner is Python; the existing Bun
page/Hub implementation remains the WS extraction candidate. The legacy
`read/run.exs` loop is Elixir, while `readers/chatgpt` is Python: running one
fixture is not evidence that the legacy loop and all materializers were ported.

A pack retaining Elixir readers must declare and deliver its Elixir/OTP dependency
explicitly. Core must not discover it accidentally through an author's mise or
neighboring checkout. Pack dependency installation and clean-Mac runtime delivery
remain acceptance work; this decision does not claim those installers exist.

## Routing decision for the first release (2026-09-07)

The first release uses the existing explicit HTTP(S) proxy backend: either clients
configure its endpoint themselves (`routing: explicit`) or TAP manages supported
macOS system proxy settings with snapshot/recovery (`routing: system`). This is a
release scope decision, not evidence that clean-Mac packaging or all TLS policy
checks have passed. Applications ignoring proxy settings are outside this path.

Local Capture is the first candidate for a second routing adapter, not a release
prerequisite. Its experimental startup reached the macOS Network Extension user
approval boundary. It is not advertised as supported process routing. Native
selection/attribution, restart/helpers, unknown-source handling, coexistence and
shared-extension lifetime must be accepted in #6 before it becomes available;
installation/permissions/update/removal acceptance belongs to #7.

Both adapters feed the same capture/mutation/injection pipeline. The common
boundary must not pretend their capabilities are identical: proxy does not
provide per-process enforcement, User-Agent is not process identity, and a
requested application constraint must fail explicitly if unsupported. Missing
native capability never silently broadens capture to all system proxy traffic.
Start with one routing choice per profile; combined modes require their own
acceptance. No custom native helper is justified without a demonstrated gap in
the existing backend. See [#3's evidence and handoff](docs/local-capture-acceptance.md).

## Dependency and packaging decisions still open

- Exact supported versions and distribution of mitmproxy/Python/Bun. The
  inspected installation reports mitmproxy **12.2.3**, embedded Python **3.14.4**
  and Bun **1.3.11**. These are baseline observations, not selected release pins.
- Standalone backend distribution versus a managed Python environment. Verify
  addon dependencies and installation/update behavior before choosing.
- Whether the Bun controller is delivered as a standalone executable and whether
  its dynamic resources, adapters and upgrades work in that form.
- JSONL/other record layout and embedded storage for consumer checkpoints. Test
  the chosen failure and retention semantics before making the format public.
- Need, implementation language and distribution boundary of a native routing
  helper. No custom driver/system extension is a prerequisite without evidence
  that the supported backend cannot meet the chosen scenario.
- Code signing/notarization and installer format for the selected macOS release
  artifacts. A developer machine's permissive setup does not satisfy acceptance.

## How to change a working choice

Record the concrete limitation, the smallest alternative, its distribution cost
and a reproducing experiment in the issue/PR. Update this document and affected
contracts in the same change. Do not silently make a private stack decision in one
task or ask for a new product decision for every ordinary library choice.

Repository ownership, process isolation and package delivery are independent
decisions. These constraints apply across the open core's packages/repositories;
they do not require all components to live in one repository.

## Sources checked for these choices

- [mitmproxy installation](https://docs.mitmproxy.org/stable/overview/installation/):
  standalone binaries contain a Python environment; addons needing extra packages
  affect the installation choice.
- [mitmproxy Local Capture](https://docs.mitmproxy.org/stable/concepts/modes/#local-capture):
  process-name/PID selection is documented for local capture; the macOS mode is
  documented as outbound capture.
- [Bun standalone executables](https://bun.sh/docs/bundler/executables): runtime
  bundling is available, but our controller's packaging has not been verified.

These upstream capabilities are candidates for verification, not claims that TAP
already uses them in the new core.
