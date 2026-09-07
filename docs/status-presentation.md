# First semantic presentation slice

Issue #52 starts with one line from `tap status` rather than a renderer for every
command. The current JSON remains the default machine output. An explicit
terminal projection is available with:

```sh
tap --profile /absolute/profile status --output terminal
```

The line takes five independently testable transformations:

```text
current status snapshot
  -> tap.status-observations/v1
  -> tap.status-summary/v1
  -> tap.status-line/v1
  -> tap.render-document/v1
  -> terminal text
```

The first stage separates known values from inspection failures. The assessment
then distinguishes a running service, a confirmed stop, a foreign listener, a
service without its listener and incomplete/contradictory observations. It keeps
the normalized observations as evidence for the claim.

The status-line projection chooses the words and importance for this command.
Lowering removes TAP-specific fields. The terminal renderer sees only generic
`status_row` blocks, marks, labels, values, details and layout width; it cannot
derive health from process or proxy fields.

This is a narrow executable seam, not the final public result contract. It does
not yet render the routing line, remediation, verbose detail, `doctor` or
`where`. The next consumer should test whether the semantic assessment and
render-document vocabulary remain useful before more block types are added.
