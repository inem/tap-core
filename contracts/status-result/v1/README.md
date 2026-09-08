# `tap.status-result/v1`

This compact result is the stable input to optional status presentations. It carries only the runtime and routing observations needed by the first product slice. Every observation explicitly distinguishes a known value from an inspection failure.

`tap status` without flags remains the existing operational snapshot. Request this contract with `tap status --output semantic-json`. Consumers must ignore neither `knowledge` nor an `unknown` reason. Capture, bridge, components, `doctor`, and `where` remain outside this version.

The fixtures cover a running direct profile, a stopped explicit profile, an inspection failure, and routing drift. Their terminal strings are examples produced from the same semantic result, not additional contract fields.
