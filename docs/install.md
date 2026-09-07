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
8. Runs `install` for a **system**-routing profile on port `18999` (Hub on `19000`).
   Because `curl | sh` cannot collect a Mac password on the pipe, it then prints
   **one** guided command: `finish-setup` (sudoers + trust this CA + `tap on`).
   Opt out of system proxy mutation with `TAP_ROUTING=explicit` (then `on` runs in the installer).


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
| `TAP_ROUTING` | `system` | `system` (default, needs one-time sudoers) or `explicit` |
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

HTTPS trust for system installs is part of `finish-setup`. Manual fallback:

```sh
open "$HOME/.tap-core/profile/certificates/mitmproxy-ca-cert.pem"
```

## Two install modes

**System (default)** — browsers pick up the macOS proxy after finish-setup, like legacy TAP.

```sh
curl -fsSL https://instll.sh/inem/tap-core | sh
bash "$HOME/.tap-core/checkout/instll/finish-setup"   # Mac password once
```

`finish-setup` explains each step, installs scoped sudoers, trusts **this**
profile CA in the System keychain, then runs `tap on`. Safe to re-run.

**Explicit** — no sudoers, no system proxy mutation; clients must point at the proxy:

```sh
curl -fsSL https://instll.sh/inem/tap-core | TAP_ROUTING=explicit bash
```

The installer prints these hints itself. `doctor` reports `sudoers.ready` for system profiles.

## Failure and ownership contract

A repeated install **refuses before downloads or replacement**. It is not an update
command. For an owned install, use the in-place updater:

```sh
TAP_REF=<branch-or-commit> bash "$HOME/.tap-core/checkout/instll/update"
```

Update stops a running profile when needed, swaps `checkout/` (and refreshes
managed bindings / pinned runtimes), rewrites the owned `tap` wrapper, and keeps
`profile/`, pack data and recorded grants (CA / sudoers). Routing mode and the CA
grant digest must stay unchanged (#6 proxy policy for this slice). Failure retains
the installation for recovery. Update is **not** purge/reinstall and does not
claim Local Capture, clean-Mac, or browser HTTPS-without-`-k`.

Choose a new `TAP_ROOT` and free `TAP_BIN_DIR` only for an independent parallel
installation. The default command name must be free: an existing file or symlink
(including a legacy TAP wrapper) is never overwritten. Environment overrides must
apply to `bash`, not just the left side of a download pipe.

Uninstall uses the recorded command location, so it does not depend on repeating `TAP_BIN_DIR`. It verifies ownership and uses Core's profile/network locks and recovery-first lifecycle through removal. A failed restore/stop, missing interpreter/configuration, or replaced command stops removal with a nonzero exit and preserves recovery files. Default uninstall removes the owned service/command and retains code/data; `TAP_PURGE=1` also removes the installation only after successful cleanup. An explicit external `TAP_PYTHON` or `TAP_BACKEND` remains untouched.

Interrupted installs retain their partial root for inspection; they do not silently overwrite it on retry. Older experimental installs lacking the ownership record/pointer require manual inspection and recovery, not a guessed purge. The installer never interprets inability to inspect a service as proof that it is stopped.

The installation and its declared code/runtimes must stay available until successful off/uninstall. This first slice has no in-place updater. Startup failure can require recovery via the retained wrapper; it is not reported as a completed install.

## Managed runtime check

Hermetic installer regressions: `python3 -m unittest tests.test_installer`.

Opt-in live cycle (empty root → components → restart → purge), reusing local
pins when provided:

```sh
python3 tools/check_installer_managed_runtime.py \
  --python "$(command -v python3)" \
  --backend "$(command -v mitmdump)" \
  --output docs/results/installer-managed-runtime.json
```

Omit `--bun` to force the installer's Bun download/extract/version path.  
`--local-checkout` is only for pre-push Core archive substitution and requires
all three runtime overrides so fake curl cannot replace Bun/Python/backend
downloads. The harness always forces `TAP_ROUTING=explicit` and retains the
install root unless uninstall cleanup is verified.

## Still open for #7 / #15

- Signed/notarized release package and locked matrix of OS/browser versions
- Automatic CA trust via `finish-setup`; verified browser HTTPS matrix and CA removal ownership still open
- System-routing recovery acceptance on a clean machine
- Full live coexistence acceptance with a parallel legacy TAP install; file/path conflicts are covered by controlled regressions
- Update channel that is not a fresh archive of a git ref

Hermetic update A→B (profile/grants/routing preserved):

```sh
python3 tools/check_installer_update.py \
  --output docs/results/installer-update-a-to-b.json
python3 -m unittest tests.test_installer
```
