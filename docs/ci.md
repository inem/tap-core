# Continuous integration (issue #24)

CI runs the repository's controlled, hermetic checks on every pull request and on
`main`, so a failing test blocks the change as a visible status check.

## Jobs

- **Python checks (no Bun)** — `python3 -m unittest discover -s tests -v` plus
  `python3 tools/check_pack_fixtures.py`. Runs on the coordinator's minimum
  Python (3.9, per `runtime-dependencies.json`) with **no Bun on PATH**, proving
  the Python reader/handler/mutator fixtures and the whole unit suite pass without
  it. The one page-module test is skipped here (`@skipUnless(shutil.which("bun"))`).
- **Page fixture (Bun 1.3.11)** — installs the pinned Bun and runs
  `tools/check_pack_fixtures.py --bun …` plus `tests.test_packs`, exercising the
  page-module → handler round trip. Bun is only the JavaScript test executor.

## What CI proves — and what it does not

CI exercises **synthetic fixtures and temporary directories only**. It does not:

- run system proxy routing, `networksetup`, trust-store changes, or `sudo`;
- load or touch the user's launchd services;
- use any account, private traffic, credential, or a neighboring checkout.

The live macOS path (`tools/check_runtime.py`, two temporary launchd profiles) is
**opt-in** and intentionally excluded. A green CI run is not clean-machine
acceptance (#7/#15) and does not establish browser/OS support beyond the tested
matrix (#2). Those remain separate, explicitly labeled evidence.
