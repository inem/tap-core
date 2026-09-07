# Combined verification of the parallel slices

This validation branch combines three independently reviewable branches based on
runtime PR #20 after its review fixes (`c300677`). It is not a fourth product
feature or an instruction to merge the feature PRs without review.

| Branch | Input commit | Responsibility |
| --- | --- | --- |
| `issue-5-pack-contract` | `07113aaa17f345da0892dd18bae877f94bd41c84` | Manifest validation and runnable pack fixtures. |
| `issue-8-writer-recovery` | `5e89bb651d92ddb2a8264801c4a71ca0101a6ce0` | Torn/failed writes, retention and shutdown recovery. |
| `issue-13-observation-errors` | `ac4ceac5ef2054895c6dcef713240463541cadaa` | Unknown OS observations and truthful diagnostic output. |

The feature branches were rebased onto the exact updated runtime base and merged
here without conflicts. The reader record shape and profile paths did not need
cross-branch changes. The pack fixture contract does not claim that the runtime
already loads packs.

Run from `integration-next-slices`:

```sh
python3 -m unittest discover -s tests -v
python3 tools/check_pack_fixtures.py --bun /absolute/path/to/bun
python3 tools/check_capture_pack_pipeline.py
python3 tools/check_runtime.py --backend /absolute/path/to/mitmdump
```

Results on 2026-09-07:

- All 73 tests passed: 29 base runtime, 19 storage, 14 pack and 11 diagnostic tests.
- Both executable pack fixtures passed; page exports called a real Python handler
  through a substituted bridge, not WebSocket.
- The combined storage/reader fixture preserved the old complete record, reported
  and removed an incomplete tail, captured a new mixed-case JSON response, and
  produced both expected reader outputs. Delivery is an explicit test invocation,
  not the future installed pack host or independent reader scheduler.
- The real launchd check on macOS 15.6.1 arm64, Python 3.9.6 and mitmdump 12.2.3
  passed with two temporary explicit profiles. Capture, KeepAlive restart and
  independent shutdown worked. System proxy settings matched before/after and
  fixture jobs were removed. See `results/next-slices-live-2026-09-07.json`.

No production TAP files, capture journal, Hub or trust store were changed. Live
system-routing transitions, clean-Mac distribution and authenticated browser WS
remain separate acceptance work.

## Follow-up review verification

The review fixes were integrated on 2026-09-07 with runtime base `56094c0`.
Published branches incorporated this base by merge, without rewriting history.
Sequential integration merges were clean; an attempted octopus merge conflicted
in listener diagnostics and was discarded before those sequential merges.

- `issue-5-pack-contract`: `808a7433423175c3fe82ef6b23bdcfb5dc72de0f`.
- `issue-8-writer-recovery`: `cde6ce385e09919751f9c250cc09ef17d3616d3f`.
- `issue-13-observation-errors`: `1a82988a6035708ce63296aa87ae532941760b90`.

All 96 tests passed: 37 runtime/lifecycle, 19 storage, 16 pack and 24 diagnostic
tests. Each feature branch also passed independently (53 pack, 56 storage,
61 diagnostics). Python pack fixtures run without Bun; only the page fixture
needs Bun. The capture/recovery/reader pipeline and both pack fixtures passed.

The updated live check passed with two temporary explicit profiles, including
removing the autoload plist on off and recreating it on on while the second
profile kept its PID and listener. See
[review live result](results/next-slices-review-live-2026-09-07.json).
A real logout/login was not performed. Startup failure cleanup and disarm-before-
cleanup ordering were tested with substituted OS operations; live system-routing
transitions, HTTPS trust, real browser WS and clean-machine acceptance remain open.
