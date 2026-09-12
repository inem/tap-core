# Doctor output

`tap doctor` prints a readable diagnosis by default. It reuses the status
summary, then displays backend, CA file/trust, proxy-rights and traffic-probe
checks, inspection errors and any copy-paste next commands. Its exit code stays
0 only when the original doctor result is healthy. The raw result is unchanged
and available with `tap doctor --output raw-json`.

`--color auto|always|never` controls terminal color. `auto` colors only a TTY.
The terminal view sanitizes observation-error text before printing it. The
original JSON remains the machine interface, including full error fields and
the `next` array. The doctor view is a presentation over the existing result;
it does not add new probes or claim CA trust where the result says
`not_verified`.
