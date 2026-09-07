# Working on TAP Core

Before implementation, read `README.md`, `TECHNOLOGY.md` and the assigned issue.
Use the issue's concrete input commits and available contract files; do not invent
an interface that an unfinished dependency is supposed to supply.

The legacy implementation is source material and behavioral evidence. Its file
layout, processes and application-specific declarations are not the required
architecture of this repository. Separate modules, processes, distribution
packages and repositories as distinct decisions.

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
