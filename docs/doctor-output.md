# Doctor output

`tap doctor` prints a readable diagnosis by default. It reuses the status
summary, then displays backend, CA file/trust, proxy-rights and traffic-probe
checks, inspection errors and any copy-paste next commands. Its exit code stays
0 only when the original doctor result is healthy. The raw result is unchanged
and available with `tap doctor --output raw-json`.

`--color auto|always|never` controls terminal color. `auto` colors only a TTY.
The terminal view sanitizes observation-error text before printing it. The
original JSON remains the machine interface, including full error fields and
the `next` array. The doctor view is a presentation over the doctor result; it
does not perform I/O itself or claim CA trust where the result says
`not_verified`. The doctor operation performs two bounded, authenticated bridge
checks when the bridge is enabled: `/health` verifies Hub protocol and PID, then
`/v1/pages` verifies the control-router endpoint. Each request has a one-second
timeout. A failed or malformed response makes doctor unhealthy and leaves the
cheaper status snapshot claim unchanged.

Doctor also separates three HTTPS claims. The ordinary traffic probe only says
that a request traversed the proxy. The **profile-CA probe** requests
`https://example.com/` through the explicit profile proxy with only that
profile's CA as its trust bundle; success therefore proves that Core decrypted
and re-signed the connection rather than merely tunnelling the public
certificate. Doctor additionally compares the peer certificate issuer CN from
curl's certificate report with the profile CA CN. The **default curl trust probe** repeats the request without
`--cacert` or `-k`; success proves trust for `/usr/bin/curl` on this host.
Browser trust is reported as a separate, not-checked acceptance surface because
browsers may cache or apply trust differently. A failed trust probe makes doctor
unhealthy and prints the owned `finish-setup` command when available.
`ca_trust_grant_recorded` preserves installer provenance independently. A green
live probe reports `ca_trust: verified_live` even when an older/manual trust
installation lacks that metadata; a recorded grant never overrides a failed
live probe. Missing grant provenance prints the owned `finish-setup` command
when Core can resolve the install root, or an exact `open <profile CA>` manual
remediation command for an older/unowned profile.
