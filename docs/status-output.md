# Status output

`tap status` keeps its original JSON output by default. Two explicit projections are available:

- `--output semantic-json` emits the versioned compact `tap.status-result/v1` contract.
- `--output terminal` interprets that result and renders two human-readable lines.

Terminal output accepts `--width N` and `--color auto|always|never`. Optional detail subtrees are omitted as a unit when they do not fit; required content causes an error rather than silent truncation. `auto` emits color only to a terminal.

The classification and presentation choices for this first slice are versioned as bundled inert JSON. Runtime and routing own separate observation, semantic and presentation fragments. A third composition fragment owns meanings that require both sections: a known unavailable runtime behind an active owned system route means `traffic broken`, a conflicting listener means traffic is routed to an unowned process, and unavailable runtime inspection means only `capture unverified`. Document assembly owns the root and terminal styles. The loader rejects overlapping observation groups, rule identifiers, fragment identifiers, styles and document-root owners before evaluation.

The Python projection kernel knows generic facts, ranked candidates, derivations, nested slots, ordering, and rendering. It contains no status-state or command-action vocabulary. The fragment manifest is internal product composition for this release, not a pack format or workflow API.

The compact result and renderer intentionally cover only status runtime/routing. Broader vocabulary, localization, arbitrary Unicode display width, optional layout below deeper nested descendants, external material loading, `doctor`, and `where` remain open.
