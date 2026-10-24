# Reactive routing: measurements (2026-10-24)

Evidence level: **bench on a profile copy**, macOS arm64, Python 3.9. This is
not live integration, and nothing was deployed.

## Why the earlier live deploys regressed

Profiling the controller under the live record mix found the CPU in
`Journal.segment_identity`. It is called on every scan, and it identifies the
active segment by hashing its first record. Its cache key included the file's
size and mtime, which change on every append, so on the active segment it
missed every time and re-read a first record that can be megabytes. The more
often a version scans, the more it pays:

- `3bee8e5` rescans every 0.25 s once anything was consumed, and filtered records
  count.
- `d5bcb68` rescans after stream writes once its 1.5 s floor has passed.

## Bench

- **Readers:** the real installed readers, with `HOME` set to a scratch dir so
  that no pack output can reach `~/tap-out`.
- **Profile:** seeded in place by the code under test. Copying a seeded profile
  breaks journal cursors, which are scoped to the data path.
- **Controller:** run in-process, without the Hub or managed services.
- **Feed:** the latest 30 MB of the live stream (live mix, mostly records no
  reader takes), byte-paced at about 100 KB/s for 180 s. After each append the
  feeder calls the code's capture-side `RouteTable.notify`, as the capture
  writer does; that has no effect on the older versions.
- **Latency probe:** a fixture reader on `https://probe.example/p/` with one
  probe record every 5 s. Latency runs from the moment the record is written to
  the moment the reader process has handled it.
- **Harness:** `bench/bench_stage2.py` in the session notes folder.

| | 3bee8e5 (live) | d5bcb68 (#267 deploy) | routing, journal fix only | routing |
|---|---|---|---|---|
| controller CPU | 13.68 % | 15.78 % | 6.11 % * | **4.86 %** |
| journal scans per reader per second | 1.58 | 0.88 | 0.89 | **0.40** |
| probe latency p50 / max | 0.535 / 1.744 s | 0.962 / 1.746 s | 0.878 / 1.612 s | **0.148 / 0.166 s** |
| probes delivered | 36/36 | 36/36 | 36/36 | 36/36 |
| reader processes per minute | 193 | 194 | 194 | 194 |

\* That run overlapped a full test-suite run on the same machine.

The real readers on this mix still receive relevant records several times a
second (usage.meters above all). That is why routed scans don't drop to zero.
Readers with nothing to do aren't woken at all, which `tests/test_reader_wake.py`
asserts directly.
