# Status output

`tap status` keeps its original JSON output by default. Two explicit projections are available:

- `--output semantic-json` emits the versioned compact `tap.status-result/v2` contract. The frozen v1 contract remains under `contracts/status-result/v1`.
- `--output terminal` interprets that result and renders runtime, routing, and capture-writer lines, followed by a cross-section alert when one is warranted.

Terminal output accepts `--width N` and `--color auto|always|never`. Optional detail subtrees are omitted as a unit when they do not fit; required content causes an error rather than silent truncation. `auto` emits color only to a terminal.

The classification and presentation choices are bundled inert JSON. Runtime, routing, and capture own separate observation, semantic and presentation fragments. Capture distinguishes a ready current writer, an absent health record, a stale record, an unhealthy current writer, and incomplete inspection; readiness does not claim that traffic is currently flowing. A composition fragment owns meanings that require multiple sections: a known unavailable runtime behind an active owned system route means `traffic broken`, a conflicting listener means traffic is routed to an unowned process, and unavailable runtime inspection means only `capture unverified`. Document assembly owns the root and terminal styles. The loader rejects overlapping observation groups, rule identifiers, fragment identifiers, styles and document-root owners before evaluation.

The Python projection kernel knows generic facts, ranked candidates, derivations, nested slots, ordering, and rendering. It contains no status-state or command-action vocabulary. The fragment manifest is internal product composition for this release, not a pack format or workflow API.

The compact result and renderer intentionally cover only status runtime, routing, and capture writer health. Bridge/components, localization, arbitrary Unicode display width, optional layout below deeper nested descendants, external material loading, `doctor`, and `where` remain open.
