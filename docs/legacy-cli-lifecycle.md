# Existing CLI behavior is part of the baseline

The running TAP already has `install`, `doctor`, `status`, `where`, `on` and `off`.
A new foreground launcher is not a replacement for this product behavior.
Extract configuration and dependencies around the existing lifecycle before
choosing to rewrite its implementation.

Source inspected: the complete legacy `tap` script, SHA-256
`a29a7ca35e3eff188cdf037eb7a59ae4518ed73fab02f09731252756b45d007e`.
The selected local snapshot has the same hash. `status`, `doctor` and `where`
were executed against the installed system. Mutating lifecycle commands were
read in full but were not executed against the user's active network.

## Command behavior found in the implementation

| Command | Existing behavior to carry into the extraction |
|---|---|
| `install` | Requires an available mitmdump; resolves the source location; links the CLI into the user's bin directory; writes a launchd job with RunAtLoad, KeepAlive and an explicit file-descriptor limit; unloads/stops an old instance and bootstraps the service; checks the service and port. On startup failure it attempts to disarm the proxy. It reports missing sudoers, proxy activation and certificate steps. It does not itself install mitmproxy or automatically grant certificate trust. |
| `on` | Starts the recorder through launchd, applies HTTP/HTTPS proxy settings and declared bypass domains to network services, then retries an actual request through the proxy. On probe failure it attempts to disarm the proxy. |
| `off` | Disables proxy settings before stopping the recorder. If it cannot confirm disarming, it refuses to kill the recorder. KeepAlive may respawn the recorder; the intended guarantee is that traffic goes directly, not that no process exists. |
| `status` | Separates recorder state and proxy routing state, reports the active service, interface coverage, port conflicts and file-descriptor pressure. Verbose mode adds protocol settings and record counts. |
| `doctor` | Checks backend availability, CA file, certificate presence, launchd registration, noninteractive proxy-control rights, port, interface coverage, an outward HTTPS request, descriptor headroom and declared/applied bypass drift. |
| `where` | Shows the CLI target, source, installed services/rights/certificate, raw stream, rotation policy and archives, blobs, auth paths, cursor, logs and reader outputs with size and recency. |

## Existing mechanisms beneath the commands

- `active_svc` derives the current network service from the default-route device;
  it is not just a hardcoded Wi-Fi selector.
- `arm_proxy` and `disarm_proxy` enumerate network services. Bypass configuration
  comes from `directions.json` plus local-address defaults.
- `start_proc` and `write_plist` use launchd. The job raises its own file-descriptor
  limit before exec, reflecting a real earlier exhaustion problem.
- `proc_up`, `port_up` and `squatter` distinguish expected recorder identity from
  port availability. Stop behavior uses a broader matching pattern than liveness.
- `mutators/.active` controls adding the mutator loader when generating the job;
  a fresh clone can therefore remain passive.
- `reload` includes preflight and service reconciliation, and currently also
  restarts the Probe and application sources. That application coupling can be
  removed without discarding the established lifecycle contract.

## Intended guarantees versus gaps to verify

Preserve the intent and observable useful behavior, rather than repeating every
current implementation detail:

- Network changes must be verified before dependent destructive steps. Current
  arm/disarm helpers primarily verify the active service, with HTTP/HTTPS combined
  by an OR check; that does not prove every service points at the intended endpoint.
- A port alone does not prove capture. Some doctor/status messages still infer
  stronger readiness from a port/process/setting than those checks establish.
- Finding a certificate by name does not prove its trust policy. The doctor's
  outward HTTPS probe uses `-k`, so it does not verify client certificate trust.
- `off` disables proxy flags; it is not an uninstall that restores a previous
  third-party proxy configuration. Ownership/restoration is a separate requirement.
- Broader stop patterns should not be copied into a multi-profile design: stopping
  one owned profile must not kill an unrelated recorder using the same port syntax.
- Install failure messages and return codes, partial network changes, missing
  permissions, and asynchronous launchd transitions need executable branch tests.

## Consequences for the current issues

- **#2:** the baseline includes these six commands and their shared helpers, not
  only capture/reader/injection hooks. This document is the source reading result;
  branch-level lifecycle tests still need implementation.
- **#4:** make the existing CLI/core sources runnable with explicit configuration
  and dependencies. An isolated foreground profile is a development facility,
  not a substitute product surface. Do not drop the six commands while extracting.
- **#7:** begin with the existing install/start/disarm/service sequence. Add
  clean-machine dependency delivery, ownership and removal behavior where missing.
- **#13:** preserve the distinction between health (`doctor`), current state
  (`status`) and locations/material (`where`); make their evidence and limitations
  accurate before introducing a new diagnostic interface.

The next safe verification should run the real command branches against controlled
OS-command adapters, checking order and failure behavior without switching the
developer's system proxy. Live install/on/off acceptance belongs on a disposable
or explicitly selected clean-machine environment.
