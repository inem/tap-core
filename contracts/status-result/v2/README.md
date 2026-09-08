# `tap.status-result/v2`

This compact result is the stable input to optional status presentations. It carries runtime, routing, and capture-writer observations. Every observation explicitly distinguishes a known value, an inspection failure, and a field that the supplied snapshot did not observe. The latter uses `{"knowledge":"unknown","reason":"not_observed","message":"observation was not supplied"}`.

`tap status` without flags remains the existing operational snapshot. Request this contract with `tap status --output semantic-json`. Consumers must ignore neither `knowledge` nor an `unknown` reason. Capture health reports writer readiness; it does not assert that traffic is currently flowing. Bridge, components, `doctor`, and `where` remain outside this version.

The fixtures cover a running direct profile, a stopped explicit profile, an inspection failure, routing drift, broken traffic, and a degraded current writer. Their terminal strings are examples produced from the same semantic result, not additional contract fields. Version 1 remains frozen beside this directory.
