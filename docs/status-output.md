# Status output

`tap status` keeps its original JSON output by default. Two explicit projections are available:

- `--output semantic-json` emits the versioned compact `tap.status-result/v4` contract. The frozen v1–v3 contracts remain under `contracts/status-result/`.
- `--output terminal` interprets that result and renders runtime, routing, capture-writer, bridge-startup and managed-component sections, followed by a cross-section alert when one is warranted.

Terminal output accepts `--width N` and `--color auto|always|never`. Optional detail subtrees are omitted as a unit when they do not fit; required content causes an error rather than silent truncation. `auto` emits color only to a terminal.

The raw operation snapshot is a carrier, not status material. The bundled
`tap.status-snapshot-adapter/v1` owns every physical snapshot path, the profile
path, the inspection-error container and collection normalization. It reduces
that carrier to `tap.status-result/v4`. A differently shaped carrier can use a
different adapter and produce the same result.

The status material begins at that versioned result. Its fragments own the
public observation vocabulary, semantic rules, composition, presentation and
document structure, but contain no snapshot paths. Projection accepts only the
result and material; it cannot read the operational snapshot or perform an
external inspection. An inspection failure and an absent value therefore stay
distinct addressed observations (`inspection_failed` and `not_observed`). Raw
diagnostic messages remain in semantic JSON for diagnosis and are not copied
automatically into terminal presentation.

The classification and presentation choices are bundled inert JSON. Runtime, routing, capture, bridge and managed components own separate vocabulary, semantic and presentation fragments. Capture distinguishes a ready current writer, an absent health record, a stale record, an unhealthy current writer, and incomplete inspection; readiness does not claim that traffic is currently flowing. Bridge first distinguishes absent, disabled, applied, unapplied, and unknown startup snapshots. Composition then refines `unapplied` with runtime state into inactive, drifted, blocked, or unverified before presentation. `applied` confirms configuration/PID agreement while preserving `hub_liveness: not_checked`; it does not claim that the Hub, handler, page, or end-to-end path is ready.

Managed components demonstrate the same chain over both scalar and collection input. The generic collection operation normalizes the raw named reader map into sorted items with stable names and one-based ordinals. Rules independently derive the page-control and each reader meaning. Presentation data creates a control section with one infrastructure line and repeated workload lines; the renderer traverses that structure without knowing what a controller or reader is. Control `ready` means current controller/configuration, fresh state and a live Hub probe when Hub is required. It deliberately ignores reader success: a failed reader is an independent workload result and does not make live page commands unavailable. It does not prove a handler, page, pack or end-to-end scenario. Reader `waiting` records a successful last finite batch, not active work or a measured-empty backlog. Bounded status error strings remain in semantic JSON for diagnosis, while terminal presentation omits them and never includes reader stdout/stderr or payload contents.

The composition fragment also owns runtime/routing consequences: a known unavailable runtime behind an active owned system route means `traffic broken`, a conflicting listener means traffic is routed to an unowned process, and unavailable runtime inspection means only `capture unverified`. Document assembly owns the root and terminal styles. The loader rejects overlapping observation groups, rule identifiers, fragment identifiers, styles and document-root owners before evaluation.

The Python projection kernel knows generic scalar and collection observations, facts, ranked candidates, derivations, nested slots, structural ordering, templates and rendering. It contains no status-state, component-state or command-action vocabulary. The fragment manifest is internal product composition for this release, not a pack format or workflow API.

The compact result and renderer now cover status runtime, routing, capture writer health, bridge startup state, the managed controller aggregate and its reader rows. Functional handler/page probes, measured reader backlog and loss, localization, arbitrary Unicode display width, external material loading, `doctor`, and `where` remain open.
