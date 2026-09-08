# `tap.status-result/v3`

This compact result is the stable input to optional status presentations. It carries runtime, routing, capture-writer, and bridge startup observations. Every observation explicitly distinguishes a known value, an inspection failure, and a field that the supplied snapshot did not observe. The latter uses `{"knowledge":"unknown","reason":"not_observed","message":"observation was not supplied"}`.

`tap status` without flags remains the existing operational snapshot. Request this contract with `tap status --output semantic-json`. Consumers must ignore neither `knowledge` nor an `unknown` reason. Capture health reports writer readiness; it does not assert that traffic is currently flowing. Bridge health reports whether the current startup snapshot matches configuration; `hub_liveness: not_checked` explicitly prevents treating it as live Hub, handler, page, or end-to-end readiness. Components, `doctor`, and `where` remain outside this version.

The fixtures cover a running direct profile, a stopped explicit profile, an inspection failure, routing drift, broken traffic, a degraded current writer, and applied/drifted/inactive bridge snapshots. Their terminal strings are examples produced from the same semantic result, not additional contract fields. Versions 1 and 2 remain frozen beside this directory.
