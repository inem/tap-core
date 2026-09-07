# Runtime extraction provenance and dependency boundary

The owner requested extraction of the generic lower layer from their private
`inem/tap` project into the MIT-licensed `inem/tap-core` repository. This transfer
is limited to the two components below; it is not a license assertion about the
rest of the private repository or future packs.

| Component | Reviewed source material | Transferred behavior |
| --- | --- | --- |
| CLI lifecycle | `tap`, SHA-256 `a29a7ca35e3eff188cdf037eb7a59ae4518ed73fab02f09731252756b45d007e` | Service startup/ownership, fd wrapper, proxy verification, on/off sequencing, diagnostics distinction. Coordination is ported to explicit Python profile/OS modules. |
| Generic capture | `capture.py`, SHA-256 `6a465a50a9276c8432319d7f2c9b616369506a08e55d5487e49172fc31841631` | Selective body capture, streaming decision, bounded queue, background JSONL writer and rotation; rewritten around per-profile state, without consumer offset coupling. |

These hashes identify the reviewed local snapshot, including uncommitted source;
they are more precise than the repository HEAD alone. The selected files carry
no separate third-party copyright/license headers, and inspected Git history
attributes them to the repository owner, Ivan Nemytchenko. The transferred core
implementation and new tests use this repository's MIT license. No private
traffic, account data, site-specific declarations, embedded secrets or raw
private source snapshot is published with the extraction.

The old installation stays the working installation until a separate migration.
The repositories do not synchronize automatically. Before incorporating further
legacy changes, run `python3 tools/check_legacy_drift.py --source /path/to/legacy`.
This read-only check compares the two selected files with the recorded snapshot
and exits nonzero on a difference. Review that difference, transfer any needed
behavior with its tests, and update baseline evidence deliberately; do not copy
over the new files or change the pin just to make the check pass. Other legacy
components are outside this two-file check. Both files still matched at the time
of this implementation's validation.

No third-party runtime is vendored in this change. The external backend is
mitmproxy **12.2.3**, whose [tagged license is MIT](https://github.com/mitmproxy/mitmproxy/blob/v12.2.3/LICENSE).
The standalone binary includes its own Python and other dependencies; complete
notices for a redistributed binary must be audited when distribution is selected.
The coordinator uses Python's standard library and macOS system commands.
See `runtime-dependencies.json` for exact tested versions and explicit
prerequisites. The development-checkout prerequisite path is not the release
installer or a completed redistribution/license inventory for bundled runtimes.
