# `tap where` output contracts

`tap where` keeps its existing unflagged JSON as the compatibility output. It
reports declared addresses from the selected profile and its installed Core
runtime; it does not inspect the filesystem.

```bash
tap --profile PROFILE where
tap --profile PROFILE where --output raw-json
tap --profile PROFILE where --output semantic-json
tap --profile PROFILE where --output terminal [--width N] [--color auto|always|never]
```

`raw-json` is the existing flat carrier. A separate
`tap.where-snapshot-adapter/v1` owns its physical keys and inspection-error
correlation. `semantic-json` emits `tap.where-result/v1`: a map of addressed
`known` or `unknown` observations with no carrier paths, labels, sections or
presentation metadata. A second, nested carrier fixture produces the identical
public result through a different adapter.

Known means only that the operation supplied an address. It makes no claim that
the path exists, is readable, is fresh, has a particular kind or belongs to a
running process. Those facts require filesystem or system observations before
projection. Optional component addresses are therefore unknown when components
are not configured; they are not reported as absent.

The terminal mode joins those addressed observations to separately validated
material declarations. Sections, labels, ownership and address roles live in
that material; physical `source.path` declarations do not. The Python projection
path does not branch on raw keys such as `config`, `backend` or `launch_agent`.
The terminal renderer sees only document slots.

Addresses are required terminal content. With no `--width`, they are emitted in
full. If an explicit width is too narrow, the command fails clearly instead of
silently dropping or truncating a path. Color affects terminal headings and
unknown markers only; JSON modes ignore terminal options and TTY state.

Projection remains optional. A projection or rendering failure exits with code
1 on stderr, and a later raw invocation still returns the machine result. The
command does not emit progress output.

## Deliberate limits

This slice does not copy the private legacy inventory. Presence, kind, bytes,
record count, archive membership, recency and owner verification remain future
operation-layer observations. The public Core also does not declare the private
organizer or a global `~/tap-out` location. The first bounded filesystem
observation is tracked separately in [#88](https://github.com/inem/tap-core/issues/88).
