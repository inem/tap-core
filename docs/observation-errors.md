# Unavailable inspection is not a stopped service

Bounded runtime follow-up for #13, based on PR #20. It does not implement the
future Hub, reader or pack diagnosis surface.

The initial extraction treated every unsuccessful `launchctl print` as an
absent service, every socket inspection error as a closed port, and incomplete
`lsof` output as definitive ownership evidence. Permission restrictions could
therefore produce an incorrect stopped/not-owned diagnosis.

The adapter now recognizes the observed macOS missing-service result (exit 113,
matching service name) separately from an unavailable domain or permission
error. Connection refusal means a closed port; other socket errors are unknown.
Listener ownership requires complete, numeric `lsof` output without warnings.
Mutating commands fail rather than act on an unknown observation.

`status` keeps independent successful observations and uses JSON `null` for
unknown values, with reasons in `inspection_errors`. `doctor` does not send its
proxy probe when ownership is unknown and cannot report healthy if an inspection
failed. Malformed or future-dated capture heartbeats do not establish health.
This is an additive development interface change; consumers must not treat
`null` as known `false`. Existing boolean observations retain their meanings.

Validation: `python3 -m unittest discover -s tests -v` includes 11 new fixture
checks for absence, permission errors, listener observations and structured
diagnostic output. The missing-service signature was observed with the real
macOS `launchctl`; permission and malformed-output cases are controlled fixtures.
The final integration run with the parallel capture/pack changes is recorded
separately. This does not establish an OS-independent error-code contract.
