# Status output

`tap status` keeps its original JSON output by default. Two explicit projections are available:

- `--output semantic-json` emits the versioned compact `tap.status-result/v1` contract.
- `--output terminal` interprets that result and renders two human-readable lines.

Terminal output accepts `--width N` and `--color auto|always|never`. Optional detail subtrees are omitted as a unit when they do not fit; required content causes an error rather than silent truncation. `auto` emits color only to a terminal.

The classification and presentation choices for this first slice are versioned as bundled inert JSON. The Python projection kernel knows generic facts, ranked candidates, derivations, nested slots, ordering, and rendering. It contains no status-state or command-action vocabulary. The bundled material is an implementation detail in this release, not a pack format or workflow API.

The compact result and renderer intentionally cover only status runtime/routing. Broader vocabulary, localization, arbitrary Unicode display width, optional layout below deeper nested descendants, external material loading, `doctor`, and `where` remain open.
