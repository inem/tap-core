# Material-driven implementation profile

This document records the implementation laws adopted by TAP Core while the
broader language is researched in the private `material-fabric` repository. It
is self-contained: building, testing, reviewing, or shipping this public
repository must not require that private checkout.

These are semantic coding standards. Formatting and ordinary local design
choices remain governed by the language and repository conventions.

## Required boundaries

For each affected path, keep these responsibilities distinguishable:

```text
host mechanism
  -> bounded receipt
  -> versioned public observation
  -> candidates / policy / selected meaning
  -> renderer-neutral document or effect plan
  -> physical output or effect attempt
```

Not every feature needs every stage. A smaller implementation may combine
adjacent stages only when their inputs, outputs, authority, and information loss
remain explicit and testable.

### Carrier and material

A carrier is a physical shape such as an operation snapshot, JSON record,
filesystem layout, process response, or terminal stream. Material owns semantic
vocabulary and relationships. Carrier keys, filesystem paths, parser behavior,
and transport failures do not become meanings implicitly.

- Carrier adapters may know physical paths and object layouts.
- Host adapters may perform bounded filesystem, process, network, proxy, or page
  operations.
- Semantic material may declare observations, candidates, policy, meanings,
  composition, and presentation.
- Projection and rendering code must not inspect the host.
- Renderers consume document primitives and must not branch on TAP domain keys.

The same public result produced from a second physical carrier is the preferred
test that this boundary is real.

### Warranted observations

Every `known` value has a supplied witness and observation authority. Preserve
these distinctions:

- declared address;
- present or absent object;
- inspection failed;
- observation not supplied or not requested;
- stale evidence;
- validity, readiness, health, and ownership by a live process.

Do not derive a stronger item from a weaker one without an explicit rule and
warrant. For example, a known path is not proof of presence; presence is not
proof that configuration is valid or used.

### Meanings and effects

Observation, candidate, selected meaning, plan, attempt, receipt, admitted fact,
proof, and presentation are different roles. A plan does not prove execution.
A timeout after dispatch is not known failure. Retrying an uncertain effect
requires explicit idempotency or reconciliation policy.

Pure semantic and presentation code receives no ambient filesystem, process,
network, credential, or clock authority. Effect runners receive only declared
capabilities and emit receipts as data.

## Where domain choices belong

Final generic infrastructure may contain code for parsing, validation,
canonicalization, matching, binding, bounded closure, candidate selection,
recursive traversal, rendering, and calls to declared host operators. Host
adapters may branch on operating-system outcomes. Generated programs may contain
whatever control flow the generator emits from checked material.

Hand-written generic code should not branch on:

- TAP states such as `running`, `stopped`, `healthy`, or `drifted`;
- specific observation names such as `config`, `backend`, or a component ID;
- command-specific labels, glyphs, section placement, or rank;
- source order used as hidden selection policy.

Repeated domain branches are evidence that a fact, relation, candidate rule,
policy, or recursive data structure is missing. Resolve that pressure by moving
the choice into versioned material or by naming the code as an imperative spike.

This is not a ban on every `if`, loop, or `case`. Review the responsibility of
the branch, not its spelling.

## Composition

Prefer open operations over behavior owned by one current container:

```text
observe(adapter, carrier)
project(material, result)
render(document, surface)
```

New carriers and implementations should participate through explicit bindings
or structural dispatch. Do not grow a central product-type conditional.

Nested results are represented by recursive slots or graph relationships. Do
not hard-code one implementation level per product depth. Extend an existing
semantic world with a bounded overlay or import by stable identity; do not copy
the whole prior material file to add one rule.

## Transformation and generation

Authoring ontologies do not need to run on every command invocation. The target
path is a sequence of independently checkable edges:

```text
strict authoring source
  -> canonical IR
  -> schema-specific elaboration
  -> normalized semantic graph
  -> target-language subset
  -> generated program or machine plan
  -> packaged executable artifact
```

Each implemented edge should expose a versioned input/output contract, declared
losses, diagnostics, a receipt, and a source map where generation occurs. A
failure names the first unsatisfied edge. Do not pretend a lossy transformation
round-trips.

Until generation is proven, small hand-written adapters and validators are
allowed. They must keep domain choices in material and remain replaceable by a
generated edge.

## Imperative spike rule

An imperative spike is acceptable when it has:

- one concrete oracle or acceptance scenario;
- a bounded implementation surface;
- fixtures preserving the learned behavior;
- an inventory of hard-coded domain decisions;
- an explicit decision to discard, retain as host mechanism, generate, or move
  those decisions into material.

Do not describe a spike as the completed material-driven implementation. Do not
use “temporary” to waive truthful error states, provenance, or effect bounds.

## Determinism and conservation

- Reordering carrier keys, supplied facts, and rules does not change unordered
  semantics.
- Order, priority, freshness, and specificity are explicit material.
- Equal-rank incompatible candidates produce a conflict.
- Missing or failed evidence produces unknown/hole residue rather than a
  plausible default.
- Recursive evaluation has a bound or termination argument.
- Every required supplied observation reaches selected meanings and output
  slots, or an edge explicitly records its omission or coarsening.
- Stable identity excludes accidental timestamps, random values, discovery
  order, and parser-dependent scalar forms.

## Evidence and review

Fixture, replay, live integration, installed-artifact, and clean-Mac checks
support different claims. State the evidence actually obtained. A second carrier
tests carrier independence; a second domain tests whether a primitive is generic.

For an affected PR, answer briefly:

1. Which user-visible intention and acceptance obligation does this satisfy?
2. What are the material and carrier at each changed boundary?
3. What witnesses each new `known` value?
4. Which distinctions are preserved or deliberately lost?
5. Which handwritten branches remain, and why are they boundary mechanics?
6. What controls ordering, conflicts, recursion, I/O, retries, and effects?
7. Can the output be regenerated and traced to its authoring source?
8. Which evidence level supports each claim?

Use these questions together with [the repository review lenses](review-planes.md).
The review lenses cover product boundaries, execution, authority, delivery, and
evidence; this profile covers the shape of the implementation inside those
boundaries.

## Current examples

`tap status` separates operation snapshots, carrier adaptation, versioned public
results, semantic/composition/presentation material, nested document slots, and
terminal bytes. A differently shaped snapshot carrier produces the same public
result.

`tap where` first treats profile values as declared addresses. Its bounded
filesystem slice separately runs `lstat`, records a sensor receipt, derives
presence and kind through data rules, and joins those meanings through a
material overlay. The host sensor branches on OS outcomes; the projection does
not branch on the `config` identity or filesystem state vocabulary.
