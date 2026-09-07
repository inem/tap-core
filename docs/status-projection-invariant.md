# Status projection conservation experiment

This branch repeats the first two `tap status` rows from a clean `origin/main`.
It does not reuse the implementation from `issue-52-status-line`. Its purpose is
to test one invariant before selecting a production presentation architecture.

## Invariant

For the selected input contract:

```text
downstream distinction => upstream warrant
upstream distinction disappears => explicit projection decision
```

The experiment checks that invariant at three concrete boundaries:

```text
tap.status-result/v1
  -> evidence + warranted runtime/routing claims
  -> nested semantic report slots
  -> traced terminal surface
  -> bytes
```

The default `status` JSON remains unchanged. The selected, versioned result can
be inspected independently:

```sh
tap --profile /absolute/profile status --output semantic-json
```

The validated terminal projection is explicit too:

```sh
tap --profile /absolute/profile status --output terminal
```

## Nested slots

The report is one small recursive structure rather than one schema for every
conceptual pass:

```text
status-list
├── status-item: runtime claim
│   ├── subject
│   ├── state-indicator
│   ├── assertion
│   └── support?                 optional subtree
│       └── evidence
└── status-item: routing claim
    ├── subject
    ├── state-indicator
    ├── assertion
    └── aside?                   optional subtree
        └── suggested-action
```

Every slot has an identifier, role and source. Structural slots own the relation
between their children. Leaves own content. The renderer does not receive the
raw status result or infer runtime/routing state.

The terminal surface retains the slot source of every meaningful token:

| Output | Owning slot |
| --- | --- |
| `●`, `○`, `✗`, `⚠` | `state-indicator` |
| supporting `·` | enclosing `support` slot |
| evidence text | nested `evidence` slot |
| `(` and `)` | enclosing `aside` slot |
| `tap on` | nested `suggested-action` slot |

Whitespace is not left unclassified. Padding and gaps are layout tokens with a
declared layout role. Surface line membership points to its `status-item`, and
the surface root points to `status-list`.

## Omission and axes

Optional material that does not fit the selected terminal width is omitted as a
whole slot subtree. The surface records the slot identifier and width decision.
Removing the same tokens without that record fails validation.

Unicode and ASCII themes choose different notation while retaining identical
semantic slot coverage:

```text
tap           ● up · PID 123    tap           + up - PID 123
browser/apps  ○ direct (tap on) browser/apps  o direct [tap on]
```

The tested axes are:

- evidence knowledge and claim derivation;
- runtime and routing domain distinctions;
- nested discourse roles;
- optionality and width-driven omission;
- visual notation through Unicode/ASCII themes;
- ANSI styling during final serialization;
- structural layout through groups, lines, padding and gaps.

The invariant is checked across combinations of state, theme and width. Color
does not participate in semantic coverage.

## What this establishes

The experiment establishes that nested slots can preserve and trace the two
legacy-shaped rows without materializing every conceptual axis as a separate
public intermediate schema. It also gives failures for invented surface sources,
silent token loss, unowned layout spacing, missing line structure, unwarranted
claims and silent claim omission.

It does not establish the final public report vocabulary, pack authoring API,
localization model or a general UI framework. A diagnostic with remediation and
a `where` path item should be tested before treating the slot vocabulary as a
shared interface. This remains fixture evidence; it is not live or clean-Mac
acceptance.
