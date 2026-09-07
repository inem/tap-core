# Working on TAP Core

Before implementation, read `README.md`, `TECHNOLOGY.md` and the assigned issue.
Use the issue's concrete input commits and available contract files; do not invent
an interface that an unfinished dependency is supposed to supply.

The legacy implementation is source material and behavioral evidence. Its file
layout, processes and application-specific declarations are not the required
architecture of this repository. Separate modules, processes, distribution
packages and repositories as distinct decisions.

The existing `install`, `doctor`, `status`, `where`, `on` and `off` commands are
part of the product baseline. Characterize their lifecycle and failure behavior
before replacing implementation. An isolated foreground test runner does not
substitute for the existing installation and network-control surface.

Follow the release requirements and working choices in `TECHNOLOGY.md`. If a
choice fails the assigned scenario, document the evidence and update the shared
decision rather than silently selecting another stack in one task. No additional
approval process is implied for routine implementation or dependency choices.

Use an isolated profile and synthetic fixtures for development. Do not use the
user's live capture or persisted action journal as test state. Preserve the
difference between fixture validation, live integration and clean-machine
acceptance in reports.

Return the commit/PR, changed interfaces, verification commands and results,
remaining limits, and any migration required by dependent issues. Do not call an
issue complete while its acceptance criteria or integration checks are missing.

For implementation and review, use [review lenses](docs/review-planes.md) for the
boundaries affected by the change. Authors identify affected lenses; reviewers
check for omissions. A separate report for every lens is not required. Distinguish
a stated slice limitation from an unmet acceptance criterion; a follow-up issue
does not by itself waive a requirement of the current task.
