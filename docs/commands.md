# Command provider contract v1

Command API 1 is the first installed binding for TAP's application-command
branch. It uses the existing immutable pack store and selected version; it does
not create a second plugin directory, daemon or discovery service.

The first two providers are built-in `where`, executed as a Core function
through the registry, and external `chatgpt search`, executed from the selected
[`chatgpt.search`](https://github.com/inem/tap-pack-chatgpt-search) pack. The external command works in a directory containing only
the pack registry and pack-owned state. `profile.json`, capture, readers, page
injection, Hub and WebSocket are not prerequisites for a command-only pack.

## Manifest declaration

A pack declares one `command` entrypoint. The host never imports or executes the
file while validating, listing or rendering help.

```json
{
  "entrypoints": {
    "command": {
      "interface": "process-argv-v1",
      "runtime": "host-python",
      "file": "command.py",
      "commands": [{
        "path": ["example", "lookup"],
        "summary": "Look up one example",
        "usage": "QUERY [--json]",
        "profile": "required"
      }]
    }
  },
  "access": {
    "origins": ["https://example.test"],
    "capabilities": ["command.execute"]
  }
}
```

`path` contains 1–8 lowercase words made from letters, digits and hyphens.
`summary` and `usage` are bounded printable declarations used by host help.
Command API 1 supports profile-local installed commands, so external declarations
use `profile: required`. A command-only pack can create that profile directory
through `pack install`; this does not install or start capture.

`process-argv-v1` defines discovery, exact argv, streams, exit and invocation
context independently of the implementation language. Runtime selection is an
explicit field, not inferred from a file extension or from Core's implementation.
The first binding is `host-python`, because Python is already the mandatory Core
runtime and therefore a command-only profile does not gain a Bun requirement.
An eventual `host-bun` binding can use the same protocol after Core has a concrete
version-to-executable inventory; v1 does not discover a global `bun` from PATH.

Arguments after the matched command path are passed as an argv array without a
shell. Core consumes only global options before the command path. The provider
owns its argument grammar, human/machine output and semantic exit codes. `--help`
as the first provider argument is reserved for host-rendered declarative help and
does not start provider code. `--dry-run` is pack-specific, not a host promise.

## Names, conflicts and diagnostics

Enabled providers share prefixes when their leaves do not conflict, so separate
packs may expose `chatgpt search` and `chatgpt chat fork`. Activation rejects an
exact duplicate, a path that is a leaf/prefix of another command, and any external
path rooted at Core recovery/management names such as `off`, `doctor` or `pack`.
There is no last-provider-wins behavior.

Root and path help show provider ID, selected version and source (`builtin` or
`pack`). A tampered or unreadable enabled pack is omitted with a diagnostic.
Ordinary Core roots bypass external discovery, so `off`, `doctor` and pack
recovery remain reachable when an external manifest or installed file is broken.

## Invocation context and execution

For the current runtime binding Core starts:

```text
<host-python> -B /immutable/selected/version/command.py ARG ...
```

stdin, stdout and stderr are inherited. The provider's nonnegative exit code is
returned unchanged; signal exits become `128 + signal`. Ctrl-C is forwarded and
reported as 130. There is no automatic retry.

`TAP_COMMAND_CONTEXT` is a compact JSON object:

```json
{
  "command_api": 1,
  "path": ["example", "lookup"],
  "provider": {"id": "example.lookup", "version": "1.2.3"},
  "runtime": "host-python",
  "profile": "/absolute/profile",
  "state_dir": "/absolute/profile/state/packs/example.lookup",
  "output_dir": "/absolute/profile/data/packs/example.lookup",
  "log_dir": "/absolute/profile/logs/packs/example.lookup",
  "config": {},
  "grants": {"origins": [], "capabilities": [], "dependencies": {}}
}
```

The host creates private state/output/log directories outside installed code and
runs with bytecode writes disabled. The context records the user's activation
decision; trusted same-user code is not an OS sandbox, and Core does not enforce
general outbound-network or filesystem access from these declarations. Pack
credentials are neither discovered nor injected by Core.

## Lifecycle and an in-flight command

Every invocation re-resolves the enabled selected version under the profile lock
and holds that lock until the subprocess exits. Update, rollback, disable and
uninstall therefore refuse clearly while a command is running. After it exits, a
change applies immediately to new invocations. There is no mixed-version call and
installed code is not removed underneath a running interpreter. Terminating a
process does not imply reversal of an external effect; retry policy remains with
the application command.

Disable removes the command from new discovery. Uninstall still requires disable
and removes installed code, while state/data/logs remain. A pack must document
whether retained paths include credentials and how to erase them intentionally.

## Build, install and verify

```sh
PYTHONPATH=/absolute/tap-core \
python3 -B -m tap_core.pack_store build /absolute/pack \
  --output /tmp/example.tap-pack

./tap --profile /absolute/command-profile pack install /tmp/example.tap-pack
./tap --profile /absolute/command-profile pack enable example.lookup \
  --version 1.2.3 \
  --grant-origin https://example.test \
  --grant-capability command.execute
./tap --profile /absolute/command-profile --help
./tap --profile /absolute/command-profile example lookup "two words"
```

Core's synthetic fixture verifies help without execution, exact argv including
shell metacharacters, streams/exit status, collisions, tampered-pack recovery and
the in-flight lifecycle lease. The separately released
[`chatgpt.search`](https://github.com/inem/tap-pack-chatgpt-search) pack adds
installed-artifact/auth/search fixtures. Neither is live-account or clean-Mac
acceptance.

## Deliberate v1 limits

Only `host-python` and profile-local installed commands are bound. There is no
global pack store, shell entrypoint, command completion schema, per-command grant
subset, sandbox, background consumer, general result renderer, marketplace or
migration of the remaining Core CLI. Result-to-presentation work continues in
#52. A second concrete runtime extends runtime selection, not the argv protocol.
