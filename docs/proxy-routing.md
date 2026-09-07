# Routing adapters: first proxy slice (#6)

The first release uses the existing proxy mechanism. It has two configurations:
clients opt into an endpoint (`explicit`), or TAP owns macOS system proxy settings
(`system`). Local Capture will be a separately accepted second mechanism. It is
not enabled or installed by this extraction.

The change removes routing-mode branches from command sequencing and moves the
existing system proxy snapshot/verification/recovery code behind a small internal
adapter. Both configurations still feed the same capture and optional injection
addons. The useful cycle remains network → capture → reader/page; this slice
separates its connection mechanism so native activation does not block it.

## Ownership and compatibility

- `Profile` keeps the same version 1 JSON, `routing` values, file locations, ports
  and labels. Existing profiles need no migration. No new install flags.
- `MacOS` still executes platform operations and manages the launchd service.
- `Lifecycle` retains start → enable routing → probe, and restore → stop.
  A restore failure prevents stopping capture; partial startup cleanup still
  happens only after recovery succeeds.
- `select_routing(profile, os_adapter)` selects the implemented proxy adapter.
  It controls listener arguments, routing enable/restore, proxy verification,
  probe, recovery state, shared mutation lock and routing-specific messages.
  Unsupported choices fail explicitly; there is no fallback to another mode.
- `SystemProxyRouting` preserves recovery snapshot format, foreign-proxy refusal,
  saved bypasses, partial rollback handling and the common per-user network lock.
  `ExplicitProxyRouting` owns no system settings and takes no shared network lock.
  Both still take the existing per-profile command lock through CLI coordination.

Python internals moved: callers of `MacOS.arm/disarm/armed` use
`select_routing(profile, os_adapter).enable/restore/verified`. Repository call
sites/tests were migrated. This is not a stable installed-pack interface or an
invitation for packs to replace host routing code. No new package dependencies.

## Diagnostics

`status` and `doctor` retain existing fields and add `routing_adapter`:

```json
{
  "version": 1,
  "mechanism": "http_proxy",
  "configuration": "explicit",
  "manages_system_settings": false,
  "process_selection": false,
  "process_attribution": false,
  "network_extension_required": false
}
```

For `system`, configuration changes to `system` and manages_system_settings to
true. These are implemented **capabilities**, not a permission/readiness check.
Existing observations remain authoritative: system_proxy_verified is true/false
for a known observation, null on inspection failure, and `not_used` for explicit.
An unavailable inspection still makes doctor unhealthy. A configured adapter is
not proof of traffic, storage health or a ready Hub.

Neither proxy variant promises process attribution or selection. Unknown app
identity is not inferred from User-Agent. `local` remains an unsupported profile
choice; native routing will need its own integration and acceptance in #6/#7.
TLS interception/tunneling, host rules and storage decisions are not implemented
by this extraction. #6 remains open for those separate results.

## Validation

On macOS 15.6.1 arm64 / Python 3.9.6 / mitmproxy 12.2.3:

- 187 local unit tests passed. The existing recovery tests use the same failure
  scenarios and assertions; only four calls moved to the new adapter boundary.
  Added checks cover old profile loading without writes, capabilities versus
  unavailable observation, unsupported-mode refusal before side effects, and CLI
  lock contention across system profiles while explicit remains independent.
- Both executable pack fixtures passed (Bun 1.3.11 for the page fixture).
- `tools/check_runtime.py` passed against two real temporary launchd profiles:
  all six commands, record/body checks, KeepAlive restart, off/on recreation,
  second-profile isolation, unchanged system settings and job cleanup.
  [Recorded result and source hashes](results/proxy-routing-live-2026-09-07.json).

The live check uses explicit loopback HTTP, no system proxy mutations or CA trust.
System-routing failure/restore is fixture-verified, not newly accepted against
live network settings. Native routing, HTTPS trust and clean-Mac packaging are
not established by these results. No production TAP or account traffic is used.

## Live in-place switching acceptance (2026-09-07)

The owner-run report in `docs/results/routing-switch-live-2026-09-07.json`
records source commit `a36b4d5` and hashes of every executed Core file. All hashes
were independently compared with that commit after the run. On macOS 15.6.1
arm64 with Python 3.9.6 / mitmproxy 12.2.3, both running and stopped
explicit/system transitions passed, same-mode retained its recovery snapshot,
and final service cleanup plus routing/bypass restoration were verified.

This is real system-preference acceptance with an explicit loopback HTTP probe.
It does not establish browser proxy discovery, trusted HTTPS, clean-Mac delivery
or managed-component switching coverage. Disabled endpoint preferences may stay
cached; the check verifies effective routing and bypass restoration. The report
contains no captured traffic or the owner's network-service/bypass configuration.
