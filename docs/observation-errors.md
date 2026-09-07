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
Only exit status 1 with both stdout and stderr empty means known absence. Any
output accompanying status 1 is an incomplete observation, even if its PID
matches the service. Successful status-0 output still distinguishes our listener
from a foreign listener or multiple owners.
Mutating commands fail rather than act on an unknown observation.

`status` keeps independent successful observations and uses JSON `null` for
unknown values, with reasons in `inspection_errors`. `doctor` does not send its
proxy probe when ownership is unknown and cannot report healthy if an inspection
failed. Malformed or future-dated capture heartbeats do not establish health.
This is an additive development interface change; consumers must not treat
`null` as known `false`. Existing boolean observations retain their meanings.

For capture, only a missing `state/capture.json` is known absence: `available`,
`healthy` and `current_process` are false. Read permission/I/O failures, invalid
UTF-8/JSON, malformed metrics and unavailable service-PID inspection instead
produce null for those three fields and a reason in `inspection_errors.capture`.
Independent observations remain available. `doctor.healthy` is always a boolean
and is false when any inspection is unknown.

A valid health record retains the fields emitted by `Writer.metrics` and adds
`available: true`. Its PID must be a positive integer; its timestamp must be a
positive finite number; `writer_alive` must be a boolean. `written`, `dropped`,
`write_errors` and `queued_bytes` must be nonnegative integers, and `last_error`
must be a string or null. All eight fields are required; booleans do not count as
integers or timestamps. Future or stale timestamps cannot establish health.
The writer's serialized shape is unchanged. Valid stopped/erroring writers are
known unhealthy, rather than an unavailable inspection.

Validation: `python3 -m unittest discover -s tests -v` includes 24 observation
tests for absence, permission errors, malformed bytes/JSON and every writer
metric type, listener observations and structured diagnostic output. One fixture
uses the actual local Writer to publish running and stopped health; synthetic
healthy records are also accepted. These tests do not start an OS service or
change routing. The missing-service signature was observed with the real
macOS `launchctl`; permission and malformed-output cases are controlled fixtures.
The final integration run with the parallel capture/pack changes is recorded
separately. This does not establish an OS-independent error-code contract.
