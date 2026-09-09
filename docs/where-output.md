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
correlation. `tap.where-result/v1` remains the frozen address-only contract: a
map of addressed `known` or `unknown` observations with no carrier paths, labels,
sections or presentation metadata. A second, nested carrier fixture produces the
identical v1 result through a different adapter.

Known means only that the operation supplied an address. It makes no claim that
the path exists, is readable, is fresh, has a particular kind or belongs to a
running process. Those facts require filesystem or system observations before
projection. Optional component addresses are therefore unknown when components
are not configured; they are not reported as absent.

`semantic-json` now emits `tap.where-result/v2`. It retains the complete v1
address map and joins one bounded observation of the selected profile's
`profile.json` address. The physical chain is deliberately separate:

```text
flat where operation carrier
  -> tap.where-snapshot-adapter/v1
  -> tap.where-result/v1 addresses

declared config address
  -> lstat sensor
  -> tap.filesystem-sensor-receipts/v1
  -> tap.where-filesystem-adapter/v1 rules
  -> tap.filesystem-observation/v1

addresses + filesystem observation
  -> tap.where-result/v2
  -> address-and-presence material overlay
  -> renderer-neutral document
  -> terminal bytes
```

The sensor is bounded to one declared target and does not scan a directory. It
does not follow symlinks. Its uniform receipt distinguishes returned regular,
symlink and other entries; known absence; and failed inspection. Adapter rules,
not the sensor, translate those receipts to public `known` or `unknown`
observations. The public result records `filesystem` as observation authority;
profile ownership remains address material.

The terminal mode joins those observations to separately validated material
declarations. Sections, labels, ownership, address roles and the meaning of
presence/kind live in that material; physical carrier paths and system calls do
not. The v2 material is an overlay on the v1 address material, so the established
address world is reused rather than copied. The Python projection path does not
branch on raw keys such as `config`, `backend` or `launch_agent`. The terminal
renderer sees only document slots.

Addresses are required terminal content. With no `--width`, they are emitted in
full. If an explicit width is too narrow, the command fails clearly instead of
silently dropping or truncating a path. Color affects terminal headings and
unknown markers only; JSON modes ignore terminal options and TTY state.

Projection remains optional. A projection or rendering failure exits with code
1 on stderr, and a later raw invocation still returns the machine result. The
command does not emit progress output.

## Deliberate limits

This slice does not copy the private legacy inventory. It observes presence and
kind for one profile configuration entry only. Bytes, record count, archive
membership, recency and owner verification remain separate possible
observations. Presence does not imply that configuration is valid, current,
safe, or used by a live process. The public Core also does not declare the
private organizer or a global `~/tap-out` location.
