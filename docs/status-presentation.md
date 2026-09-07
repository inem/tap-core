# Status meaning and terminal notation experiment

Issue #52 starts with the first two lines from `tap status`. The current JSON
remains the default machine output. The experimental projection is explicit:

```sh
tap --profile /absolute/profile status --output terminal
```

The experiment has two halves. The first accumulates evidence and domain
meaning independently for runtime and routing:

```text
current status snapshot
  +-> tap.status-observations/v1
  |     -> tap.runtime-assessment/v1
  |
  +-> tap.routing-observations/v1
        -> tap.routing-assessment/v1
```

An observation says what was inspected and whether its value is known. An
assessment makes an evidence-backed claim about TAP, such as `running`,
`direct`, `client_opt_in` or `recovery_required`. Neither assessment decides
what should appear on screen.

The second half starts from the accumulated claims and passes through its own
semantic chain:

```text
runtime assessment + routing assessment
  -> tap.status-meaning/v1
  -> tap.status-messages/v1
  -> tap.visual-document/v1
  -> tap.render-document/v1
  -> tap.terminal-plan/v1
  -> terminal text
```

`status-meaning` selects what the command should communicate: subjects,
assertions, supporting meanings and suggested actions. `status-messages`
chooses human words while preserving discourse relations such as supporting
evidence, explanation and suggested action.

`visual-document` assigns visible roles. A state has abstract attention and an
indicator form; supporting material is secondary; a suggested action is an
aside with parenthetical enclosure. It contains no concrete status glyphs,
separator dots or parentheses.

`render-document` adds grouping and alignment. `terminal-plan` is the first
artifact allowed to select terminal notation:

| Prior meaning | Terminal notation |
| --- | --- |
| positive active state | solid indicator `●` |
| neutral inactive state | hollow indicator `○` |
| failed state | cross indicator `✗` |
| warning state | warning indicator `⚠` |
| supporting relation | middle-dot separator |
| secondary suggested action | parenthetical text |

The serializer applies ANSI styling and emits terminal text only after those
choices have been resolved. Tests assert that concrete notation does not leak
into observations, assessments, meaning, messages, visual semantics or layout.

The current result deliberately stops after two compact rows. It does not yet
compose cross-claim messages such as “proxy armed but tap is down”, verbose
detail, `doctor` or `where`. Each new case should first prove that the existing
boundaries can express its meanings before adding another visual primitive.
