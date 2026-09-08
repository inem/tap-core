# Status output

`tap status` keeps its original JSON output by default. Two explicit projections are available:

- `--output semantic-json` emits the versioned compact `tap.status-result/v3` contract. The frozen v1 and v2 contracts remain under `contracts/status-result/`.
- `--output terminal` interprets that result and renders runtime, routing, capture-writer, and bridge-startup lines, followed by a cross-section alert when one is warranted.

Terminal output accepts `--width N` and `--color auto|always|never`. Optional detail subtrees are omitted as a unit when they do not fit; required content causes an error rather than silent truncation. `auto` emits color only to a terminal.

The classification and presentation choices are bundled inert JSON. Runtime, routing, capture, and bridge own separate observation, semantic and presentation fragments. Capture distinguishes a ready current writer, an absent health record, a stale record, an unhealthy current writer, and incomplete inspection; readiness does not claim that traffic is currently flowing. Bridge first distinguishes absent, disabled, applied, unapplied, and unknown startup snapshots. Composition then refines `unapplied` with runtime state into inactive, drifted, blocked, or unverified before presentation. `applied` confirms configuration/PID agreement while preserving `hub_liveness: not_checked`; it does not claim that the Hub, handler, page, or end-to-end path is ready. The same composition fragment owns runtime/routing consequences: a known unavailable runtime behind an active owned system route means `traffic broken`, a conflicting listener means traffic is routed to an unowned process, and unavailable runtime inspection means only `capture unverified`. Document assembly owns the root and terminal styles. The loader rejects overlapping observation groups, rule identifiers, fragment identifiers, styles and document-root owners before evaluation.

The Python projection kernel knows generic facts, ranked candidates, derivations, nested slots, ordering, and rendering. It contains no status-state or command-action vocabulary. The fragment manifest is internal product composition for this release, not a pack format or workflow API.

The compact result and renderer intentionally cover only status runtime, routing, capture writer health, and bridge startup state. Managed components, functional Hub/page/handler probes, localization, arbitrary Unicode display width, optional layout below deeper nested descendants, external material loading, `doctor`, and `where` remain open.
