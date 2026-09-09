# Background commands

A command pack may declare a finite periodic invocation on a command:

```json
"schedule": {"interval_seconds": 30, "timeout_seconds": 300}
```

The pack must request and receive `background.run`, as well as
`command.execute`. No arguments are supplied; configuration comes from the
normal command context. Intervals are 10–86400 seconds and timeouts 1–3600.
This is an extension of the command manifest: earlier Core validators reject it.

Core owns one LaunchAgent per profile. Every tick discovers enabled, verified,
selected command providers and runs due commands sequentially with the ordinary
profile lease. Version/config changes become effective on the next tick. A busy
profile retries later; this is periodic best-effort work, not an exact clock.
Intervals start after completion. Commands must tolerate repeated execution.

Capture `off` does not stop disk/background work. Pack disable removes future
invocations; removing the last scheduled pack unloads the background LaunchAgent.
Profile uninstall also unloads it, retaining data. Profile on/install reconcile
registration. The CLI owns reconciliation; direct PackStore use alone does not
register OS services. A failed registration reports that pack state was saved;
retry the pack operation to reconcile.

The worker inherits the profile lease into the command subprocess. Timeout kills
its process group and reports `unknown` (exit 124): an external effect may already
have happened. Commands must remain finite and must not daemonize.
A hard-killed worker can leave a child holding the lease until that child exits;
this slice does not promise exactly-once delivery or crash recovery of effects.

`tap status` includes background state, selected versions, start/finish times,
exit code and next eligible time. The registration flag means the plist exists;
it is not a claim that the process is currently running. Per-command private logs
are under `logs/background/`; logs larger than 1 MiB are removed between runs,
not limited while a command is writing.

Validation: isolated installed commands exercise due time, version change,
disable/uninstall, timeout and registration removal. A separate macOS check
exercises actual launchd on a temporary bare profile.
