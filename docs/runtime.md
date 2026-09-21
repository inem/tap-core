# First profile-scoped runtime

The checkout lifecycle slice of #4 is complete. This is a runnable source
checkout, not the clean-machine package required by #7/#15.

## Run the explicit development profile

Prerequisites: macOS, Python 3.9+ and an explicitly installed **mitmdump 12.2.3**.
The recorded run used macOS 15.6.1 arm64, the system development Python 3.9.6,
and the standalone backend with embedded Python 3.14.4. The command checks the
backend version before installation or startup. No pip packages, Elixir, Bun,
application accounts or neighboring checkout are required.

From this checkout:

```sh
./tap --profile "$PWD/.tap-dev" install \
  --backend "$(command -v mitmdump)" \
  --port 18999 --routing explicit
./tap --profile "$PWD/.tap-dev" on
curl --noproxy '' --proxy http://127.0.0.1:18999 http://example.com/
./tap --profile "$PWD/.tap-dev" status
./tap --profile "$PWD/.tap-dev" doctor
./tap --profile "$PWD/.tap-dev" where
./tap --profile "$PWD/.tap-dev" off
./tap --profile "$PWD/.tap-dev" uninstall
```

Choose another port if occupied; TAP refuses to kill or replace its owner.
`--probe-url` can select a controlled HTTP(S) endpoint at installation; the
default is `http://example.com/`. This probe checks a successful HTTP response
through the proxy, not storage completion or browser certificate trust.

`install` registers a unique user LaunchAgent and starts capture, without
changing system routing. `on` starts/verifies that service and probes traffic.
Repeating `install` for an existing owned profile is a repair operation: it
uses the saved configuration, restores direct routing before replacing the
owned jobs, and preserves captured data, certificates, pack state and reader
checkpoints. Creation options are accepted only for a new profile.
An explicit profile requires clients to select its proxy endpoint. Its `off`
stops the service and removes its autoload plist until the next `on`; clients still explicitly configured to use that endpoint must
stop using it themselves. There is no claim that an explicit client's traffic
automatically goes direct after shutdown.

`uninstall` removes the profile's LaunchAgent after stopping it. It retains
profile configuration, captured data and certificates. `on` can register that
retained profile again. Uninstall before deleting/moving the checkout or profile:
this developer installation intentionally refers to its checkout and backend.
It does not install a global command or modify an existing `tap` symlink.

## Existing mechanics and deliberate changes

| Existing behavior | Extracted behavior |
| --- | --- |
| Start capture through launchd; KeepAlive and raised fd limit | Preserved; shell wrapper runs `ulimit -n 65536` before exec. Failure to raise the limit stops startup. |
| Start → arm → real proxy request | Preserved for system routing. Explicit routing performs start → proxy request. |
| Disarm before stopping capture | Preserved for system routing; failed recovery prevents stop. |
| Port liveness and service identity matter | Both verified, including listener PID matching this exact launchd job. |
| Operate across network services and preserve recovery state | Both HTTP and HTTPS verified on every enabled service, including inactive adapters. Existing bypass entries are saved for recovery and restored on off; the active TAP route keeps only the profile's declared passthrough list (pinning clients such as iCloud), so historical exceptions and local development hosts do not silently disable capture/injection. |
| Active service works while inactive services are unarmed | Reported as an explicit rescue/partial state with exact mismatches. It is not release-grade `system` routing and does not relax recovery ownership. |
| `install` can swallow startup/plist errors | Errors return nonzero. Failed startup restores routing before removing its job and plist; cleanup failures remain explicit. |
| Failed arm/rollback can claim safety | Arm failure attempts recovery; failed recovery retains the snapshot and reports failure without stopping capture. |
| Broad process-pattern termination | Removed; only the exact profile job is booted out. An occupied foreign port is an error. |
| `off` allows the recorder to respawn | The extracted command explicitly removes its autoload plist, boots out its own job and waits for its PID to exit, so this profile stays stopped until on. |
| `off` cuts live keep-alive clients into a closed port (#176) | After the stop, the port is held for a bounded, loopback-only `CONNECT` drain so straggling clients tunnel direct instead of erroring; in-process (never collides with a later `on`), `TAP_OFF_DRAIN_SECONDS` tunes it (`0` disables). |
| Capture rotation resets a shared reader offset | Removed. No reader runs in this slice; capture does not own consumer progress. |

CLI coordination is implemented in Python using the working choice in
`TECHNOLOGY.md`. The reason is explicit JSON profiles, atomic recovery files,
plist serialization/argument quoting and command locks using one standard
library. This is a port of the characterized lifecycle, not a foreground runner.
`MacOS` contains OS calls; `Lifecycle` contains sequencing and error handling;
`Profile` contains paths/configuration. [Routing adapters](proxy-routing.md) own
proxy enable/restore, verification and the shared network lock. These are internal modules, not proposed
package boundaries or a new public plugin protocol.

The earlier legacy fixture remains available in `tools/characterize_legacy_cli.py`.
`tests/test_runtime.py` tests corrected semantics in the extracted implementation
instead of freezing the documented legacy bugs as requirements.

## Paths and isolation

Each explicit profile directory owns:

- `profile.json`: version 1, backend path, port, routing, probe URL and optional addons.
- `data/stream.jsonl` and retained archives: capture only.
- `state/proxy-before.json`: recovery snapshot for a system-routing profile.
- `state/capture.json`: writer PID, heartbeat, queue size, drops and write errors.
- `certificates/`: independent backend configuration and CA; no trust-store mutation.
- `logs/capture.log`: backend and writer diagnostics.
- `command.lock`: short profile observations and mutations are serialized.
- `state/command-execution/command.lock`: installed provider execution is
  serialized with pack mutation without blocking capture/network lifecycle.

The LaunchAgent is `~/Library/LaunchAgents/com.tap.core.<profile-path-hash>.plist`.
All listeners bind to `127.0.0.1`. System-routing mutations also hold a common
per-user lock under `~/Library/Application Support/TAP Core/network-control`.
The existing `com.tap` job, port 8899, `~/.tap`, reader state and live Hub are not
used as development state.

`--addon /absolute/path/to/addon.py` is an explicit opt-in for an additional
trusted mitmproxy addon. It is not a pack format or sandbox. No app addon is
enabled by default, and optional addons remain declared user dependencies.

## System-routing mode and recovery limits

`install --routing system` selects the legacy networksetup mechanism. It does
not arm the system proxy until `on`. This mode needs noninteractive permission
for the specific networksetup proxy commands; the CLI uses `sudo -n`, reports
permission failure, and does not modify sudoers. Permission provisioning and
the packaged installer remain #7 work.

Before arming, the runtime saves both proxy settings and bypasses for all enabled
services. The active TAP route clears domain bypasses; pre-existing bypasses
remain recovery state, not active capture exclusions. This prevents an old system
exception such as `github.com` or a blanket loopback exception such as
`localhost` from making a page invisible to injection while TAP is on. Protection
against routing back into TAP's own listener belongs at the profile route
boundary, not in a host-wide proxy bypass.
It refuses to replace an already enabled system proxy, including an existing TAP
installation. `off` is the network escape hatch: it restores and verifies the
saved routing state before waiting for an ordinary profile command to drain, then
repeats the idempotent recovery under the normal lifecycle locks before stopping
the job. If bounded cleanup cannot obtain the profile lease, the command reports
that networking was restored and cleanup remains pending.
Recovery failure leaves the snapshot and service available;
after correcting the OS/permission problem, run `off` again. It refuses to
overwrite an unrelated proxy enabled after its snapshot was created.

Graceful off (#176): restoring the saved routing points *new* connections
direct, but a long-lived client (the desktop app running this session, any
process that cached the system proxy or is holding keep-alive sockets to the
listener port) would otherwise hit a closed port the instant the backend stops.
So after the job is stopped `off` keeps the port answering for a bounded window
as a plain loopback CONNECT tunnel to the origin — no interception, no capture —
so those clients migrate to the now-direct route instead of erroring. The window
is in-process and bounded, so it can never outlive `off` and collide with a later
`on` (which refuses to start on an occupied port). It is `CONNECT`-only (every
client we care about is HTTPS; a non-CONNECT request gets a clean 501), binds
loopback only, and is best-effort: failing to bind never fails `off`. The window
defaults to 10s; `TAP_OFF_DRAIN_SECONDS` overrides it, and `0` restores the
previous instant-stop behavior. This does not stop capturing any client while
TAP is on — it only smooths the stop transition (contrast with `passthrough`,
which exempts a host from capture entirely).

Setting a proxy endpoint can also enable it. To avoid briefly sending traffic
through an old server during recovery, this adapter disables the proxy directly
and leaves its server/port populated. It restores bypasses and disabled routing,
not byte-for-byte preferences. Added/removed network
services during an active session can require manual recovery; a newly added
service pointing at this profile without a snapshot prevents shutdown. Full
interface-change automation, proxy-auth restoration, power-loss recovery and
uninstall acceptance remain unresolved. Do not infer those guarantees from the
controlled adapter tests.

## Capture and diagnostics

The generic capture retains JSON/text and streams binary/SSE. Capture/storage
limits live in `profile.capture` (and `TAP_CORE_CAPTURE` for the launchd job).
Defaults preserve the previous hardcoded bounds; each limit has a distinct owner:

| Limit | Default | Owner / effect at the boundary |
| --- | --- | --- |
| `stream_large_bodies` | 4 MiB | mitmproxy backend: larger bodies are streamed; Capture sees `streamed`, not a buffered body. Not a process-memory cap. |
| `max_body_bytes` | 12 MiB | Capture: omit retained/decoded text before or after `get_text` (`oversize` / `oversize_decoded`). Combined request+response must fit journal `MAX_RECORD_BYTES` (32 MiB). The same cap applies to request bodies independently retained by `request_body_paths`. Unknown Content-Length alone does not force streaming; backend `stream_large_bodies` bounds large chunked bodies. |
| `queue_slots` / `queue_bytes` | 64 / 16 MiB | Writer submit queue only; overflow increments `dropped` and does not invent a journal gap. |
| `segment_bytes` / `keep_rolls` | 128 MiB / 3 | On-disk rotation of `stream.jsonl` archives; not a total disk quota. |

[`stream_large_bodies`](https://docs.mitmproxy.org/stable/overview/features/#streaming)
is passed through from the profile into `write_plist`. Streamed responses produce
metadata without body access. Oversized queue submissions drop with a visible
counter. Serialization/storage occur on the writer thread. Write errors are
logged and included in health output; shutdown attempts to drain queued records
with a bounded wait.

This does not establish a global process-memory cap, a bound on decompression
cost before `max_body_bytes` is checked, lossless power-failure storage or
complete end-to-end storage failure acceptance (#8). Reader gap/replay
semantics are documented in #9. New HTTP records use [capture record v1](capture-records.md), retaining the
main legacy fields and adding identity and explicit body dispositions. Old JSONL
is read-compatible; acknowledgement and scheduling are implemented in #28/#43.

`status` reports service identity, listener ownership and writer health without
issuing an HTTP request. `doctor` additionally checks the pinned backend and
makes a proxy request; it returns nonzero on a failed operational check. Writer
health must belong to the current PID and have a recent heartbeat. CA file
presence is separate from certificate trust, which remains explicitly unverified.
The detailed legacy fd-pressure display and prerequisite provisioning guidance
remain diagnosis/installer follow-ups in #13/#7. `where` only reports paths.

## Validation

```sh
python3 -m unittest discover -s tests -v
python3 tools/check_runtime.py --backend /absolute/path/to/mitmdump
```

The controlled tests cover real routing enable/restore methods through substituted OS
operations, lifecycle ordering, partial failures, crash recovery, foreign
listeners, profile/argument isolation, capture streaming, retention and visible
write failures. The opt-in live check creates two temporary user LaunchAgents,
runs the six commands, captures controlled loopback HTTP, verifies KeepAlive
restart and checks that stopping one profile preserves the other. It compares
system settings before/after and removes its own jobs even on failure.

See [the recorded live result](runtime-live-2026-09-07.json) and
[dependency declarations](../runtime-dependencies.json). The live check does not
arm system routing, trust a CA, validate HTTPS/browser traffic or prove clean-Mac
installation. System-routing transitions are currently fixture-verified only.

### Installed migration lifecycle gate

`tools/check_migration_lifecycle.py` is the owner-run gate for the real installed
system-routing profile. It records the active network service and complete
starting proxy state, refuses legacy port `8899`, runs `off -> on -> doctor`,
checks listener ownership, explicit and ordinary HTTPS requests, `scutil`, and a
real headless Chrome/Chromium page load, then always runs `off` and verifies
direct access. A timestamped JSON report is written under `docs/results/`, even
after a failed check.

Run it from an owner terminal after reviewing the profile and browser paths:

```sh
python3 tools/check_migration_lifecycle.py \
  --allow-system-routing \
  --profile "$HOME/.tap-core/profile" \
  --browser "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
```

The gate intentionally finishes with the profile off. Named, understood doctor
gates may be admitted with repeated `--expected-limitation NAME` (for example
`components` or `inspection:sudoers`); all other unhealthy gates fail the run.
A cleanup failure leaves the profile and
recovery snapshot intact and records `rollback_error` in the result.
