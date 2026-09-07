# Extraction start: behavior, boundaries and evidence

Status: initial proposal for issue #2, not a finished runtime architecture.

The legacy implementation is useful source material. Its directory tree, service
launcher and application-specific declarations are not requirements for the new
core. Preserve useful verified behavior; document defects rather than preserving
them as intended semantics.

## Evidence available now

`tools/characterize_legacy.py --source PATH` exercises trusted legacy source using
synthetic data and temporary state. It does not change the system proxy, read real
traffic or start the production Hub. The capture module's fixed output paths are
redirected in the harness; its hooks and background writer execute unmodified.
The reader executes as a separate subprocess with a temporary output directory.

The recorded run in `legacy-characterization-2026-09-07.json` establishes:

- JSON and HTML records retain their bodies.
- Binary and SSE records are marked streamed without accessing their bodies.
- The existing reader stores an initial and a changed version. Repeating A after B
  leaves the latest pointer at B: a known gap, not a successful acceptance result.
- The bootstrap is injected once on each of two allowed synthetic origins and is
  absent on a denied origin; a padded nonce is retained.
- A synthetic same-origin Probe request routes to the local endpoint and removes
  the original credentials and query token.

These are fixture results. They do not establish live WS delivery, crash recovery,
per-application routing, or installation on a clean Mac. Earlier live HTTP checks
also demonstrated capture and script insertion, but they are separate observations
and are not reproduced by this harness.

## Boundaries to preserve or change

| Observed coupling | Proposal for the first release | Reason / evidence |
|---|---|---|
| Capture resets the reader's shared byte offset during rotation. | Capture owns recording and retention. Each consumer owns its progress against an explicit stream identity. | A writer cannot safely advance/reset independent consumers; archived unread records need an explicit replay policy. Issues #8/#9. |
| The reader runner imports site readers and periodically starts ChatGPT materializers. | The host schedules registered readers; a pack supplies site matching, parsing and materialization. | Core startup must work without any site integration. Issues #4/#5/#9. |
| The mutator loader imports Python modules inside mitmproxy. | Keep an explicit Python hook boundary where required by this backend; do not require downstream readers to use Python. | Backend hook mechanics constrain execution, not repository or pack ownership. Issues #5/#10. |
| The Probe launcher starts Hub plus application-specific sources. | Launch the generic bridge independently; enable application sources through their packs. | A generic page-to-local example should not launch a ChatGPT workflow. Issues #11/#14. |
| The Hub contains transport, persistence, adapter loading and workflow coordination. | Establish the smallest page/controller round trip before choosing which additional coordination belongs in core. | Existence of Actions/Needs/Flows does not make all of them prerequisites for release. Issues #11/#12. |
| Site UI libraries already live outside TAP. | Consume them through packs when appropriate, subject to license review. | Separate library development need not be collapsed into the core repository. |

## Repository and distribution decisions

Only the initial public repository is decided: `tap-core`.

- The initial host, capture integration and installer can develop together here.
- Page runtime and local bridge need explicit versioned boundaries. Whether they
  become separate packages or repositories remains open until the first round trip
  and update path identify an independent release requirement.
- Application packs are independently supplied and may be maintained in separate
  private repositories. Core tests use open synthetic packs, not paid source.
- Existing UI libraries need not move repositories.
- Process attribution/routing may require a platform-specific helper. Issue #3
  must establish its actual mechanism, permissions and lifecycle before deciding
  its distribution boundary.

A module, a process, an installable package and a repository are different choices.
Create a repository boundary for an independent release/ownership/access need;
verify separability through explicit dependencies first.

## First implementation slice

The [existing CLI lifecycle](legacy-cli-lifecycle.md) is part of the behavior to
preserve: `install`, `doctor`, `status`, `where`, `on` and `off`. Issue #4 must
extract that surface with explicit configuration; a new foreground launcher alone
does not replace it.

After the lifecycle baseline and dependency review, a supporting isolated
development check should deliver this behavior:

1. Start a core profile with explicit local state and no application packs.
2. Send a controlled HTTP request through it and obtain a saved record.
3. Stop the profile without changing the user's existing TAP installation.

This check should not require the full Hub, a site account, marketplace or a final
pack API. It is an early verification step inside the extraction, not the complete
user-facing runtime milestone. Issue #5 supplies the pack contract and fixture
declarations; full reader and page/WS integration is accepted in #14.

## Work still required before closing #2

- Establish the license and provenance of each source file selected for public
  migration. No LICENSE/COPYING/NOTICE was found in the inspected source projects;
  the new repository's MIT license does not itself settle legacy-file provenance.
- Select and pin distribution dependencies after checking their licenses and
  installation constraints.
- Establish the first supported macOS/CPU/browser matrix on clean machines.
  The developer machine alone is not that matrix.
- Accept or revise these proposed boundaries using the capture/reader and
  injection/WS scenarios, then pass the chosen baseline and decisions to #4/#5.

The full private source snapshot and path-bearing manifest remain local. The
public report includes only the hashes of exercised files and synthetic results.
