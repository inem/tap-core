# Passive session-header observation for command packs

A command entrypoint may declare:
```json
"session_headers": {
  "path_prefix": "/backend-api/",
  "headers": ["cookie", "authorization", "user-agent"]
}
```
It requires a granted `session.observe` capability, one exact HTTPS origin
(default port), and normal command activation. This is trusted local pack access,
not an execution sandbox. Only these three header names are supported.

The Core proxy observes existing browser requests. It sends no requests itself,
does not create a login flow and never puts these headers in capture records or
the page bridge. A bounded background queue (16 x 64 KiB maximum) keeps disk and
pack discovery off the request hook. Oversized, busy and full-queue observations
are discarded; the next matching browser request can refresh state. This is
best-effort refresh, not a complete credential history.

Current selection and grants are rechecked under the profile lease before any
write. Files are atomic, mode 0600, below the receiving pack's private
`state/packs/<id>/auth/<hostname>.<header>`. No destination override is accepted
by the observer. Disable prevents subsequent writes, including queued requests.
Previously observed state is retained by the existing uninstall policy.
Absent headers leave previous values unchanged, matching the migration addon.

An already-running proxy needs one restart to load the new Core addon.
Subsequent grant/selection changes are discovered without proxy restart.
Header-bearing commands should use their own state/auth directory; sharing an
existing credential directory is not implied by pack installation.

This narrow extension replaces the local ChatGPT organizer migration addon;
Core contains no ChatGPT hostname, API path or organizer logic. Synthetic unit
tests cover scope, permissions, private storage, disable and queue limits.
