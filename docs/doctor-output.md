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
