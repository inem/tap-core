# First profile-scoped runtime

Preparatory implementation for #4; #4 remains open. This is a runnable source
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
An explicit profile requires clients to select its proxy endpoint. Its `off`
stops the service; clients still explicitly configured to use that endpoint must
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
| Operate across network services and preserve bypasses | Both HTTP and HTTPS verified on every enabled service, including inactive adapters. Existing bypass entries are combined with local bypasses and restored on off. |
| `install` can swallow startup/plist errors | Errors now return nonzero. Plist failure prevents bootstrap. |
| Failed arm/rollback can claim safety | Arm failure attempts recovery; failed recovery retains the snapshot and reports failure without stopping capture. |
| Broad process-pattern termination | Removed; only the exact profile job is booted out. An occupied foreign port is an error. |
| `off` allows the recorder to respawn | The extracted command explicitly boots out its own job and waits for its PID to exit, so this profile stays stopped until on. |
| Capture rotation resets a shared reader offset | Removed. No reader runs in this slice; capture does not own consumer progress. |

CLI coordination is implemented in Python using the working choice in
`TECHNOLOGY.md`. The reason is explicit JSON profiles, atomic recovery files,
plist serialization/argument quoting and command locks using one standard
library. This is a port of the characterized lifecycle, not a foreground runner.
`MacOS` contains OS calls; `Lifecycle` contains sequencing and error handling;
`Profile` contains paths/configuration. These are internal modules, not proposed
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
- `command.lock`: concurrent profile mutations are serialized.

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
services. It refuses to replace an already enabled system proxy, including an
existing TAP installation. `off` restores the saved routing state and bypasses and verifies them before
stopping the job. Recovery failure leaves the snapshot and service available;
after correcting the OS/permission problem, run `off` again. It refuses to
overwrite an unrelated proxy enabled after its snapshot was created.

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

The generic capture retains JSON/text and streams binary/SSE. The backend's
[`stream_large_bodies`](https://docs.mitmproxy.org/stable/overview/features/#streaming)
cutoff is 4 MiB, including large JSON. Streamed responses produce metadata without
body access. The writer retains up to three 128 MiB archives plus the current
file; the queue has 64 slots and a conservative 16 MiB payload budget. Oversized
queue submissions drop with a visible counter. Serialization/storage occur on
the writer thread. Write errors are logged and included in health output;
shutdown attempts to drain queued records with a bounded wait.

This does not establish a global process-memory cap, a bound on decompression,
lossless power-failure storage or finalized retention/replay semantics. Those
belong to #8/#9. The current JSONL retains the legacy record fields, with
`req_body_kept` added when the response body is retained. It is not yet a stable
versioned consumer contract.

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

The 29 controlled tests cover real arm/disarm methods through substituted OS
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
