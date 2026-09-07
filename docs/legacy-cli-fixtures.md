# Existing CLI command-branch fixtures

Preparatory work for #4, based on the lifecycle audit in #16. No runtime has
been ported and no installed TAP files have been changed.

## Reproduce

From this checkout, with Python 3.10+ and Bash:

```sh
python3 tools/characterize_legacy_cli.py --source /path/to/trusted/legacy-snapshot
```

The caller supplies the private source; it is not included in this repository.
The harness accepts only the reviewed `tap` SHA-256
`a29a7ca35e3eff188cdf037eb7a59ae4518ed73fab02f09731252756b45d007e`.
Inspect changed source and reachable operations before updating that pin. There
is no override for executing an unreviewed version.

The harness inserts fixture functions before the original command dispatcher in
a temporary local copy. The `on`, `off` and `install` branches execute unchanged.
Lifecycle helpers return scenario-controlled results. OS calls, plist writes,
directory creation and symlink installation are replaced with recording
functions. No backend process, launchd operation, certificate lookup or network
request is performed. `HOME` is retained; the install branch's `mkdir` and `ln`
calls do not perform writes. The temporary source copy is deleted afterwards.

This is a trusted-source fixture, not an execution sandbox. It establishes
branch order, return codes and output claims. It does **not** establish the
correctness of helper implementations or actual macOS state transitions.

## Observed results

The [recorded report](legacy-cli-characterization-2026-09-07.json) contains 15
matched scenarios, including six scenarios explicitly classified as known gaps.
A successful harness exit means that observed legacy behavior matches the
characterization, including those gaps. It does not mean the product meets its
release requirements.

| Scenario | Observed behavior |
| --- | --- |
| `on`, successful start | Start → arm → traffic probe. |
| `on`, failed start | Exit 1 without attempting to arm. |
| `on`, failed arm | Exit 1 without rollback; output claims the proxy is off without establishing that. |
| `on`, failed traffic probe | Attempt to disarm and exit 1. If disarm also fails, output still claims a safe rollback. |
| `off`, failed disarm | Exit 1 without stopping capture. |
| `off`, successful disarm | Stop capture afterwards. A failed stop does not change exit 0; recorder respawn is explicitly allowed. |
| `install`, absent backend | Exit 1 before installation operations. |
| `install`, successful checks | Write plist → bootout → stop → bootstrap → verify port and service. |
| `install`, failed startup | Attempt to disarm, but exit 0. A failed disarm still produces a direct-traffic claim. |
| `install`, live port without service | Reject as failed startup, but still exit 0. |
| `install`, failed plist write | Continue to bootstrap; successful later checks produce a success claim. |
| `install`, missing setup prerequisites | Print permission, enablement and certificate instructions; do not perform those actions. |

The plist failure case supplies successful subsequent checks to establish that
the write error is ignored; it is not evidence that launchd actually loaded a
stale plist. Likewise, helper failure codes model failures without claiming a
specific partially configured network state.

## How this informs the extraction

Preserve start-before-arm, traffic verification, disarm-before-stop, the service
check during installation and the distinction between traffic being direct and
the recorder being stopped. Treat misleading safety claims and swallowed
installation failures as defects to correct, not compatibility requirements.
When those are corrected, update the corresponding characterization and add
acceptance checks for the corrected behavior.

Next in #4: make paths and OS operations explicit in the extracted code, then
exercise the actual start/stop/arm/disarm helpers against controlled adapters.
That must include partial HTTP/HTTPS failure, an inactive service that remains
armed, launchd delays and port ownership. The current helper doubles do not
cover these. The existing `doctor`, `status`, `where`, `reload`, generated plist
contents, bypass rules, clean installation and live traffic remain outside this
fixture's coverage. #4 remains open.

This work is a separate stacked branch on #16. Review or changes to the baseline
can be incorporated before the runtime extraction without mixing its diff into
the PR currently under review.
