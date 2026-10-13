# Opt-in live wire observations

First slice of [#240](https://github.com/inem/tap-core/issues/240).
Core publishes HTTP body fragments and WebSocket messages while traffic passes
through the proxy. It does not parse SSE, decompress bodies, identify chats,
create application messages or change the active conversation. Those meanings
belong to a provider pack. The existing completed HTTP journal/readers are unchanged.

## Opt-in and lifecycle

An explicitly configured profile can add `live_routes` to its existing capture
configuration (the other required capture limits stay present):

```json
{
  "live_routes": [
    {"origin": "https://fixture.example", "path_prefix": "/responses",
     "metadata_headers": ["x-fixture-thread"]}
  ]
}
```

Default: no live routes, no live publisher. At most 64 exact origins/path prefixes
and 16 explicitly named lowercase metadata headers per route. Matching uses the
parsed origin and path, not substrings in a query. Queries are not published.
Credential header names (authorization, cookies, token/API-key headers) are
rejected by both policy and observation validation. The authenticated controller
can still see private request/response body material within the opted-in routes;
this is not a body-redaction layer or an execution sandbox.

Live capture requires an enabled managed bridge/Hub. Configuration is a proxy
startup snapshot: stop this profile before changing routes, then start it through
the existing lifecycle. Pack-declared route policy is not introduced in this
slice. Configure the profile explicitly; do not widen every pack's grants.

Selected HTTP response bodies are forced into pass-through streaming, including
responses with absent Content-Type. They therefore cannot also be buffered for
body mutation or retention by the ordinary response hook. Buffered request bodies
are observed at `request()`, before upstream forwarding; already streamed request
bodies are explicitly unavailable. No request-body streaming hook is introduced.
HTTP 101 upgrades keep their handshake behavior; WS messages use their own hooks.
Previously installed response stream callbacks/iterables are preserved.

The publisher runs on a bounded worker owned by Capture. No disk I/O, subprocess,
JSON serialization or network wait runs in the wire observation hooks. Shutdown
abandons pending observations, counts them as loss, and waits at most one second
for the worker. Observation failures never intentionally block the upstream.

## Local transport

The existing profile Hub exposes `/v1/capture/live`. Both directions require
`Authorization: Bearer <profile component token>`. The page/bridge token does not
authorize this endpoint, and pages cannot publish through a Signal envelope.
No controller token is sent into page code. This is a trusted local-controller
surface; per-pack producer credentials and page audiences remain separate work.

- `POST`: one validated observation; success 202 with a cursor. Body reading is
  capped at 128 KiB. Malformed/unsupported input returns 400.
- `GET` with `Accept: text/event-stream`: subscription, at most eight clients.
- Other authenticated `GET`: bounded feed health, without retained packet bodies.

The SSE stream starts with `event: ready`, whose JSON contains `schema`, `cursor`
and `volatile: true`. This is status, **not acknowledgement of replayed packets**.
`event: observation` carries an observation and an SSE `id` cursor. Save a cursor
only after consuming that observation; pass it opaquely as `Last-Event-ID`.

The Hub retains at most 128 records in memory, with a new epoch each process
start. A bounded matching cursor can replay retained observations. Matching
`record_id` plus identical content is idempotent within that retained window;
conflicting content is refused. No durable storage or exactly-once claim is made.

`event: gap` reports `publisher_loss`, `subscriber_overflow`, `retention_gap`,
`replay_overflow`, `epoch_changed`, `future_cursor` or `invalid_cursor`. A slow
subscriber is given a final gap and closed. A bad/expired replay cursor gets a gap
and continues from new observations. The consumer must reconcile authoritative
message state rather than concatenating across a gap; an automatic reconnect
alone does not repair missing content. Invalid cursor consumers must discard the
bad cursor after reconciliation, instead of indefinitely retrying it.

## Observation v1

The executable validator is `tap_core/live-capture.mjs:validateObservation`.
Synthetic examples are in `fixtures/capture-live/v1.jsonl`.

Every observation contains:

| Field | Meaning |
| --- | --- |
| `schema` | Exact `tap.capture.live/v1`; independent of HTTP journal v1. |
| `record_id` | Observation UUID; deduplication within the bounded retained window. |
| `flow_id` | One observed exchange UUID, not a chat, turn or source application ID. |
| `sequence` | Positive increasing per-flow sequence, assigned before enqueue; jumps expose loss. |
| `ts` | Unix observation time; ordering is sequence-based, not wall-clock-based. |
| `transport` | `http` or `websocket`. |
| `direction` | `request` (client -> upstream) or `response` (upstream -> client). |
| `phase` | `start`, `data`, `end` or `error`. |

Data uses `data`, a base64 string for at most 48 KiB of wire bytes, not UTF-8 text
or an SSE event. Consumers reassemble/decode across boundaries. HTTP compression
is preserved; start metadata includes `content_encoding`. WS data is the backend's
reassembled message payload (unmasked and decompressed by its WS layer), not raw
TCP/frame bytes. Large WS messages are split into data observations sharing a
positive `message` number and `opcode` 1/text or 2/binary. A WS `end` with those
fields terminates that message; `end` without them terminates the socket.

HTTP request `start.metadata`: method, exact origin, path (max 4096 characters),
`path_truncated`, allowlisted `headers`, `headers_omitted`, content_type,
content_encoding and `body_available`. Headers are capped at 8192 UTF-8 bytes in
aggregate. Request bodies over 12 MiB or already streamed/unavailable are omitted
with `error.reason` `observation_limit` or `body_unavailable`, followed by `end`.
HTTP response start metadata contains status, content_type and content_encoding.
Type/encoding strings are bounded; they do not establish provider/source identity.
`error.reason=transport_error` omits the backend's potentially private error text.

**End is a transport boundary, not model-message completion.** An adapter must
recognize provider completion/interruption/error events and retain unknown states.
An HTTP stream can contain many provider messages; a WS socket can contain many
responses/turns. Repeated model input is not evidence of newly sent user messages.

An optional positive `dropped_before` reports observations lost before a later
successful publication; it is a publisher-wide count, not a specific message loss.
Conservatively mark affected live state incomplete until reconciled. If the Hub
never recovers/no further publication occurs, loss remains in producer health,
not a fictitious delivered event.

## Bounds and evidence

Publisher queue: 128 bounded records. Hook data fragments: 48 KiB. HTTP publication
timeout: 0.5 seconds per socket operation on the worker. Subscriber JS queue:
64 events plus a final gap. Retention: 128 records, each at most 128 KiB. These are
component bounds, not an OS/network total memory quota. Producer counters live in
`state/live-capture.json` (sent, queue_dropped, publication_failed,
unreported_loss, queued, stopped). This slice does not yet project these counters
into the public doctor/status views.

```sh
python3 -m unittest discover -s tests -q
bun tests/live_capture_feed.mjs
python3 tools/check_live_capture.py --backend /absolute/mitmdump --bun /absolute/bun
```

The last command uses actual isolated backend/Hub listeners and synthetic
upstreams held open after their first event/frame. It checks pre-completion
delivery, missing content-type, byte preservation, both WS directions,
authentication/invalid input, upstream credential exclusion, Hub failure and loss
reporting on recovery. It does not change system routing, use user data, prove
provider message identity, install a pack, or establish clean-machine acceptance.

Remaining integration: managed opt-in lifecycle live run, provider routing and
identity evidence, adapter message contract, panel subscription, pack policy,
public health projection and durable recovery if required. See dependent
[Work #5](https://github.com/inem/tap-pack-chatgpt-work/issues/5) and
[Observer #23](https://github.com/inem/active-chat-observer/issues/23).
