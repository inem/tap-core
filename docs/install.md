# One-line install (#7)

First delivery slice for a clean Mac. Not a signed release artifact (#15).

## Install

After this lands on `main`:

```sh
curl -fsSL instll.sh/inem/tap-core | sh
```

While testing a branch:

```sh
TAP_REF=issue-7-installer curl -fsSL \
  https://raw.githubusercontent.com/inem/tap-core/refs/heads/issue-7-installer/instll/install | sh
```

What it does:

1. Downloads pinned mitmproxy **12.2.3** for the machine architecture into `~/.tap-core/backend`.
2. Fetches this repository at `TAP_REF` (default `main`) into `~/.tap-core/checkout`.
3. Writes `~/.local/bin/tap` → profile-scoped CLI wrapper.
4. Runs `install` + `on` for an **explicit** profile on port `18999` (no system proxy mutation, no keychain trust).

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
| `TAP_ROUTING` | `explicit` | `explicit` or `system` |
| `TAP_SKIP_START` | `0` | `1` = place files only |
| `TAP_BIN_DIR` | `~/.local/bin` | Where the `tap` wrapper is written |
| `TAP_BACKEND_VERSION` | `12.2.3` | Required mitmproxy version |
| `TAP_BACKEND` | (unset) | Absolute mitmdump to reuse instead of downloading into `TAP_ROOT` |

## After install

```sh
export PATH="$HOME/.local/bin:$PATH"
tap doctor
open "$HOME/.tap-core/profile/certificates/mitmproxy-ca-cert.pem"   # trust CA manually
# Point a browser user-data-dir at http://127.0.0.1:18999
```

## Still open for #7 / #15

- Signed/notarized release package and locked matrix of OS/browser versions
- Automatic CA trust install/removal with verified HTTPS clients (no `-k`)
- System-routing recovery acceptance on a clean machine
- Conflict with a parallel legacy TAP install
- Update channel that is not a fresh archive of a git ref
