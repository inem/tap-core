# One-line install (#7)

First delivery slice for a **bare** Mac (arm64 or Intel). Not a signed release
artifact (#15).

## Prerequisites on the machine

Only macOS base tools: `curl`, `tar`, `bash`.  
**Not required:** Homebrew, Xcode, CLT, system Python, git, mise.

## Install

After this lands on `main`:

```sh
curl -fsSL instll.sh/inem/tap-core | sh
```

While testing a branch (needs instll.sh `@ref` support deployed):

```sh
curl -fsSL instll.sh/inem/tap-core@issue-7-installer | sh
```

Fallback without `@ref`:

```sh
TAP_REF=issue-7-installer curl -fsSL \
  https://raw.githubusercontent.com/inem/tap-core/issue-7-installer/instll/install | sh
```

What it does:

1. Picks architecture (`arm64` / `x86_64`).
2. Downloads portable CPython into `~/.tap-core/python` for this CPU
   (override with `TAP_PYTHON` if you already have one). Avoids Xcode CLT stubs.
3. Downloads pinned mitmproxy **12.2.3** for that CPU into `~/.tap-core/backend`.
4. Fetches this repository at `TAP_REF` into `~/.tap-core/checkout`.
5. Writes `~/.local/bin/tap`.
6. Runs `install` + `on` for a **system**-routing profile on port `18999`
   (arms macOS proxy on `on`; `off` restores). Override with `TAP_ROUTING=explicit`.

## Uninstall

```sh
curl -fsSL instll.sh/inem/tap-core/uninstall | sh
TAP_PURGE=1 curl -fsSL instll.sh/inem/tap-core/uninstall | sh   # also delete ~/.tap-core
```

## Environment

| Variable | Default | Meaning |
| --- | --- | --- |
| `TAP_ROOT` | `~/.tap-core` | Install root |
| `TAP_REF` | `main` | GitHub ref for the checkout archive |
| `TAP_PORT` | `18999` | Profile proxy port |
| `TAP_ROUTING` | `system` | `explicit` or `system` |
| `TAP_SKIP_START` | `0` | `1` = place files only |
| `TAP_BIN_DIR` | `~/.local/bin` | Where the `tap` wrapper is written |
| `TAP_BACKEND_VERSION` | `12.2.3` | Required mitmproxy version |
| `TAP_BACKEND` | (unset) | Absolute mitmdump to reuse |
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

Point a browser at the proxy only if you used `TAP_ROUTING=explicit`. With the
default `system` mode, Safari/Chrome pick up the macOS proxy after `on`.

To switch an already-installed profile without reinstall:

```sh
tap off
tap routing system
tap on
```

## Still open for #7 / #15

- Signed/notarized release package and locked matrix of OS/browser versions
- Automatic CA trust install/removal with verified HTTPS clients (no `-k`)
- System-routing recovery acceptance on a clean machine
- Conflict with a parallel legacy TAP install
- Update channel that is not a fresh archive of a git ref
