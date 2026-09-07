# One-line install (#7)

First delivery slice for a **bare** Mac (arm64 or Intel). Not a signed release
artifact (#15).

## Prerequisites on the machine

Only macOS base tools: `curl`, `tar`, `bash`.  
**Not required:** Homebrew, Xcode, CLT, system Python, git, mise.

## Install

After this lands on `main`:

```sh
curl -fsSL instll.sh/inem/tap-core | bash
```

While testing a branch (needs instll.sh `@ref` support deployed):

```sh
curl -fsSL instll.sh/inem/tap-core@issue-7-installer | TAP_REF=issue-7-installer bash
```

Fallback without `@ref`:

```sh
curl -fsSL \
  https://raw.githubusercontent.com/inem/tap-core/issue-7-installer/instll/install | TAP_REF=issue-7-installer bash
```

What it does:

1. Picks architecture (`arm64` / `x86_64`).
2. Downloads portable CPython into `~/.tap-core/python` for this CPU
   (override with `TAP_PYTHON` if you already have one). Avoids Xcode CLT stubs.
3. Downloads pinned Bun **1.3.11** into `~/.tap-core/bun` (override with `TAP_BUN`).
4. Downloads pinned mitmproxy **12.2.3** for that CPU into `~/.tap-core/backend`.
5. Resolves `TAP_REF` to one exact commit, downloads that checkout and records the commit in `install.json`.
6. Writes managed bridge/components under `~/.tap-core/managed/` with absolute paths
   only inside the install root (Hub, example reader/handlers from the checkout).
7. Refuses an existing install root or occupied command path; creates an owned `~/.local/bin/tap`.
8. Runs `install` + `on` for an **explicit** profile on port `18999` (Hub on `19000`)
   with those managed bindings, then `tap doctor` and a Hub HTTP/WS smoke.
   Clients opt into the proxy, so the existing system proxy is preserved.
   Set `TAP_ROUTING=system` explicitly where system routing is available; this needs
   administrative access and refuses another enabled proxy.

## Uninstall

```sh
bash "$HOME/.tap-core/checkout/instll/uninstall"
TAP_PURGE=1 bash "$HOME/.tap-core/checkout/instll/uninstall"  # also delete owned data
```

## Environment

| Variable | Default | Meaning |
| --- | --- | --- |
| `TAP_ROOT` | `~/.tap-core` | Install root |
| `TAP_REF` | `main` | GitHub ref for the checkout archive |
| `TAP_PORT` | `18999` | Profile proxy port |
| `TAP_HUB_PORT` | `TAP_PORT + 1` | Managed Hub listen port |
| `TAP_ROUTING` | `explicit` | `explicit` or `system` |
| `TAP_SKIP_START` | `0` | `1` = place files only |
| `TAP_BIN_DIR` | `~/.local/bin` | Where the `tap` wrapper is written |
| `TAP_BACKEND_VERSION` | `12.2.3` | Required mitmproxy version |
| `TAP_BACKEND` | (unset) | Absolute mitmdump to reuse |
| `TAP_BUN_VERSION` | `1.3.11` | Required Bun version |
| `TAP_BUN` | (unset) | Absolute `bun` to reuse |
| `TAP_PYTHON` | (unset) | Absolute `python3` to reuse |
| `TAP_PYTHON_VERSION` | `3.12.14` | Portable CPython version when downloading |
| `TAP_PYTHON_BUILD` | `20260901` | python-build-standalone release tag |

## After install

The installer itself runs `uname -m` and `tap doctor` at the end (with
`PATH` including `~/.local/bin` for that process).

For later shells:

```sh
export PATH="$HOME/.local/bin:$PATH"
tap doctor
```

Trust the CA manually:

```sh
open "$HOME/.tap-core/profile/certificates/mitmproxy-ca-cert.pem"
```

Point a dedicated client at the proxy for default explicit routing. This installer does not establish CA trust. System routing is opt-in and is not a supported clean-Mac claim yet.

The in-place routing command from merged #47 is `tap routing set explicit|system`. It preserves the profile and restores the previous network state before leaving system mode. This installer adds no second configuration command; existing `install` never rewrites a saved profile.

## Failure and ownership contract

A repeated install **refuses before downloads or replacement**. It is not an update command. Choose a new `TAP_ROOT` and free `TAP_BIN_DIR` for an independent installation. The default command name must be free: an existing file or symlink (including a legacy TAP wrapper) is never overwritten. Environment overrides must apply to `bash`, not just the left side of a download pipe.

Uninstall uses the recorded command location, so it does not depend on repeating `TAP_BIN_DIR`. It verifies ownership and uses Core's profile/network locks and recovery-first lifecycle through removal. A failed restore/stop, missing interpreter/configuration, or replaced command stops removal with a nonzero exit and preserves recovery files. Default uninstall removes the owned service/command and retains code/data; `TAP_PURGE=1` also removes the installation only after successful cleanup. An explicit external `TAP_PYTHON` or `TAP_BACKEND` remains untouched.

Interrupted installs retain their partial root for inspection; they do not silently overwrite it on retry. Older experimental installs lacking the ownership record/pointer require manual inspection and recovery, not a guessed purge. The installer never interprets inability to inspect a service as proof that it is stopped.

The installation and its declared code/runtimes must stay available until successful off/uninstall. This first slice has no in-place updater. Startup failure can require recovery via the retained wrapper; it is not reported as a completed install.

## Managed runtime check

Hermetic installer regressions: `python3 -m unittest tests.test_installer`.

Opt-in live cycle (empty root → components → restart → purge), reusing local
pins when provided:

```sh
python3 tools/check_installer_managed_runtime.py \
  --local-checkout \
  --python "$(command -v python3)" \
  --backend "$(command -v mitmdump)" \
  --bun "$(command -v bun)" \
  --output /tmp/installer-managed.json
```

Omit `--local-checkout` after the branch is published to fetch the commit archive.
Omit the runtime overrides to force the installer's own downloads.

## Still open for #7 / #15

- Signed/notarized release package and locked matrix of OS/browser versions
- Automatic CA trust install/removal with verified HTTPS clients (no `-k`) — next delivery slice
- In-place update preserving profile/packs/data — next delivery slice
- System-routing recovery acceptance on a clean machine
- Full live coexistence acceptance with a parallel legacy TAP install; file/path conflicts are covered by controlled regressions
- Update channel that is not a fresh archive of a git ref
