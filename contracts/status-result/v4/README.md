# `tap.status-result/v4`

This compact result is the stable input to optional status presentations. It adds managed component controller observations and a normalized collection of reader observations to v3. Every scalar and item field distinguishes a known value, an inspection failure, and a field that the supplied snapshot did not observe.

`component_readers` distinguishes an unavailable collection from a known empty collection. Known named-map entries are sorted by name and receive stable one-based ordinals for structural ordering. Names remain the item identity; ordinals only describe this result's order.

Component `healthy` is the existing controller aggregate: current configuration/process, a fresh controller report, reader health, and a live Hub probe when the selected configuration requires Hub. It does not assert handler, page, pack, or end-to-end readiness. Reader `waiting` means the last finite batch succeeded and the controller is waiting for more input; it does not claim active processing or zero backlog. Error strings are available to semantic consumers but the bundled terminal summary does not print them or reader payload/log contents.

The fixtures cover absent components, a ready controller with multiple readers, a retrying reader, and controller/reader failure. Versions 1–3 remain frozen beside this directory.
