# Live page development protocol

The installed pack lifecycle and the local authoring lifecycle accept different
materials. `pack install` accepts a frozen external artifact. The development
channel addresses one already-connected page and intentionally permits a local
controller to inspect or change it without creating a pack version.

```text
local controller -> authenticated Hub endpoint -> page WebSocket -> live document
```

The page bootstrap exposes two Core-owned development operations:

- `tap.dev.inspect` accepts a selector and a result limit. It returns a bounded
  projection of matching elements, including text, markup, role, classes,
  rectangle and computed visibility.
- `tap.dev.execute` accepts a JavaScript async-function body of at most 64 KiB.
  It runs in the selected top-level document and returns its JSON value. The
  bridge-injected CSP nonce authorizes the temporary script element; completion
  or failure returns through the existing command/result WS envelope.

The installed CLI gives these operations a development vocabulary:

```sh
tap dev allow https://example.com
tap dev pages
tap dev inspect PAGE_ID 'main' --limit 20
tap dev execute PAGE_ID --source 'return document.title'
tap dev execute PAGE_ID --file /absolute/path/to/experiment.js
```

`tap dev allow` accepts one exact canonical HTTP(S) origin. It persists the
same-user development grant and refreshes the effective runtime snapshot. A
running Hub reads that snapshot for new bootstrap and WebSocket requests, so
the command does not restart capture, readers, the controller or system
routing. Reload a document that was already open before the grant so its next
HTML response receives the bootstrap. Repeating the command is a no-op.

The page runtime exposes `mode: development` through `TapBridge.status()`, so
development UI can render a distinct state. If the optional `tap.inspector` pack
is already enabled, Core binds its page resource to development origins and the
inspector lamp appears there. This local page-only binding does not expand the
pack's installed grants or handler authority. Other page packs remain scoped to
their installed origin grants.

The caller must possess the profile-local component token, and every command is
addressed to one volatile page/session. Calls are bounded by the existing input,
output, concurrency and timeout limits. They are not replayed after disconnect.
Arbitrary development source is trusted same-user code and may mutate site state;
reloading the selected page is its general reset operation.

An enabled managed browser/control bridge starts its Hub even without installed
handlers and offers the development channel to every allowed, non-excluded
origin. A reader-only profile explicitly disables the bridge and remains hubless.

The protocol does not yet watch a source tree, publish development resource
generations, suspend one overlay, or freeze a successful generation into an
immutable pack candidate. Those are authoring lifecycle work under #41. None
requires stopping capture or changing system routing.
