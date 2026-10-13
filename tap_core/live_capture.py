"""Opt-in wire observations. Bytes go unchanged; provider meaning stays in packs.

Only the bounded worker talks to the local authenticated Hub. There is no live
observation journal/replay promise here; a lost publication is reported on the
next successful one and in health, while ordinary HTTP capture remains separate.
"""
import base64
import http.client
import json
import queue
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

SCHEMA = 'tap.capture.live/v1'
FRAGMENT_BYTES = 48 * 1024
MAX_METADATA_BYTES = 8192
MAX_REQUEST_BYTES = 12 * 1024 * 1024
FORBIDDEN_HEADERS = {'authorization', 'proxy-authorization', 'cookie', 'set-cookie',
                     'x-api-key', 'api-key', 'x-auth-token'}


def validate_routes(routes):
    if not isinstance(routes, (list, tuple)) or len(routes) > 64:
        raise ValueError('capture.live_routes must contain at most 64 routes')
    result = []
    for route in routes:
        if not isinstance(route, dict) or set(route) - {'origin', 'path_prefix', 'metadata_headers'}:
            raise ValueError('Invalid live capture route')
        origin, prefix = route.get('origin'), route.get('path_prefix')
        if not isinstance(origin, str) or len(origin) > 512:
            raise ValueError('Invalid live capture origin')
        parsed = urlsplit(origin)
        try:
            port = parsed.port
        except ValueError:
            raise ValueError('Invalid live capture origin port') from None
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname
                or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment
                or origin != f'{parsed.scheme}://{parsed.netloc.lower()}'
                or (parsed.scheme, port) in (('http', 80), ('https', 443))):
            raise ValueError('Live capture needs an exact origin without credentials/path')
        if not isinstance(prefix, str) or not prefix.startswith('/') or len(prefix) > 512 or '?' in prefix or '#' in prefix:
            raise ValueError('Invalid live capture path_prefix')
        headers = route.get('metadata_headers', [])
        if (not isinstance(headers, (list, tuple)) or len(headers) > 16
                or any(not isinstance(h, str) or not h or len(h) > 64
                       or not all(c.isascii() and (c.isalnum() or c == '-') for c in h)
                       or h != h.lower() or h in FORBIDDEN_HEADERS
                       or any(word in h for word in ('authorization', 'cookie', 'token', 'api-key')) for h in headers)):
            raise ValueError('Invalid/credential live capture metadata header')
        result.append({'origin': origin, 'path_prefix': prefix, 'metadata_headers': list(headers)})
    return result


class Publisher:
    def __init__(self, root, port, *, slots=128):
        self.root, self.port = Path(root), port
        self.pending = queue.Queue(maxsize=slots)
        self.stopped = threading.Event()
        self.lock = threading.Lock()
        self.dropped = self.sent = self.failed = self.unreported = 0
        self.thread = threading.Thread(target=self.run, daemon=True, name='tap-live-capture')
        self.thread.start()

    def submit(self, record):
        # Records are bounded by fragment and metadata limits before enqueue.
        with self.lock:
            if self.stopped.is_set():
                self.dropped += 1
                self.unreported += 1
                return False
            try:
                self.pending.put_nowait(record)
                return True
            except queue.Full:
                self.dropped += 1
                self.unreported += 1
                return False

    def run(self):
        connection = None
        token = None
        try:
            while not self.stopped.is_set():
                try:
                    record = self.pending.get(timeout=.1)
                except queue.Empty:
                    continue
                if self.stopped.is_set():
                    self.pending.task_done()
                    with self.lock:
                        self.dropped += 1
                        self.unreported += 1
                    break
                with self.lock:
                    lost = self.unreported
                body = {**record, 'dropped_before': lost} if lost else record
                try:
                    # Read only on the worker, retry after token rotation/refusal.
                    if token is None:
                        token = (self.root / 'state/component-token').read_text().strip()
                    if connection is None:
                        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=.5)
                    connection.request('POST', '/v1/capture/live', json.dumps(body),
                                       {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
                    response = connection.getresponse()
                    response.read(4096)
                    if response.status != 202:
                        raise OSError('publication refused')
                    with self.lock:
                        self.sent += 1
                        self.unreported -= lost
                except (OSError, http.client.HTTPException, ValueError):
                    if connection is not None:
                        connection.close()
                    connection = None
                    token = None
                    with self.lock:
                        self.failed += 1
                        self.unreported += 1
                finally:
                    self.pending.task_done()
                self.health()
        finally:
            if connection is not None:
                connection.close()
            self.health()

    def health(self):
        with self.lock:
            data = {'schema': SCHEMA, 'sent': self.sent, 'queue_dropped': self.dropped,
                    'publication_failed': self.failed, 'unreported_loss': self.unreported,
                    'queued': self.pending.qsize(), 'stopped': self.stopped.is_set()}
        try:
            path = self.root / 'state/live-capture.json'
            temporary = path.with_suffix('.tmp')
            temporary.write_text(json.dumps(data))
            temporary.replace(path)
        except OSError:
            pass  # Never report keys, packet content or upstream credentials.

    def close(self):
        with self.lock:
            self.stopped.set()
        discarded = 0
        while True:
            try:
                self.pending.get_nowait()
                self.pending.task_done()
                discarded += 1
            except queue.Empty:
                break
        with self.lock:
            self.dropped += discarded
            self.unreported += discarded
        # No long proxy shutdown wait if the Hub is unavailable.
        self.thread.join(timeout=1)
        self.health()


class Observer:
    def __init__(self, publisher, routes):
        self.publisher = publisher
        self.routes = validate_routes(routes)

    def route(self, flow):
        url = urlsplit(flow.request.url)
        origin = f'{url.scheme}://{url.netloc.lower()}'
        return next((r for r in self.routes if origin == r['origin'] and (url.path or '/').startswith(r['path_prefix'])), None)

    def state(self, flow):
        return flow.metadata.get('tap_live_capture')

    def emit(self, flow, transport, direction, phase, **fields):
        state = self.state(flow)
        if state is None:
            return
        state['sequence'] += 1
        self.publisher.submit({'schema': SCHEMA, 'record_id': str(uuid.uuid4()),
                               'flow_id': state['id'], 'sequence': state['sequence'],
                               'ts': time.time(), 'transport': transport,
                               'direction': direction, 'phase': phase, **fields})

    def data(self, flow, transport, direction, content, **fields):
        for at in range(0, len(content), FRAGMENT_BYTES):
            self.emit(flow, transport, direction, 'data',
                      data=base64.b64encode(content[at:at + FRAGMENT_BYTES]).decode('ascii'), **fields)

    def request(self, flow):
        route = self.route(flow)
        if route is None:
            return
        flow.metadata['tap_live_capture'] = {'id': str(uuid.uuid4()), 'sequence': 0, 'ws_message': 0}
        request = flow.request
        headers = {name: request.headers[name] for name in route['metadata_headers'] if name in request.headers}
        # Bounded allowlisted metadata. Missing identity remains missing.
        kept = {}
        used = 0
        for name, value in headers.items():
            cost = len(value.encode('utf-8')) + len(name)
            if used + cost <= MAX_METADATA_BYTES:
                kept[name] = value
                used += cost
        parsed = urlsplit(request.url)
        available = request.raw_content is not None and not request.stream
        within_limit = available and len(request.raw_content) <= MAX_REQUEST_BYTES
        self.emit(flow, 'http', 'request', 'start', metadata={
            'method': request.method[:32], 'origin': route['origin'], 'path': parsed.path[:4096] or '/',
            'path_truncated': len(parsed.path) > 4096,
            'headers': kept, 'headers_omitted': len(kept) != len(headers),
            'content_type': request.headers.get('content-type', '')[:512],
            'content_encoding': request.headers.get('content-encoding', '')[:128],
            'body_available': within_limit})
        if within_limit:
            self.data(flow, 'http', 'request', request.raw_content)
        else:
            self.emit(flow, 'http', 'request', 'error', reason='observation_limit' if available else 'body_unavailable')
        self.emit(flow, 'http', 'request', 'end')

    def responseheaders(self, flow):
        if self.state(flow) is None or flow.response is None:
            return
        response = flow.response
        self.emit(flow, 'http', 'response', 'start', metadata={
            'status': response.status_code, 'content_type': response.headers.get('content-type', '')[:512],
            'content_encoding': response.headers.get('content-encoding', '')[:128]})
        if response.status_code == 101:
            return
        # Explicit opt-in forces pass-through streaming even with missing ctype.
        # If another addon owns a transform, observe the bytes AFTER that transform.
        previous = response.stream if callable(response.stream) else None
        def observe(chunk):
            result = previous(chunk) if previous else chunk
            if isinstance(result, bytes):
                self.data(flow, 'http', 'response', result)
                if not chunk:
                    self.emit(flow, 'http', 'response', 'end')
                return result
            # mitmproxy also allows a stream transform to return an iterable.
            def fragments():
                for part in result:
                    self.data(flow, 'http', 'response', part)
                    yield part
                if not chunk:
                    self.emit(flow, 'http', 'response', 'end')
            return fragments()
        response.stream = observe

    def error(self, flow):
        self.emit(flow, 'http', 'response', 'error', reason='transport_error')

    def websocket_start(self, flow):
        self.emit(flow, 'websocket', 'response', 'start')

    def websocket_message(self, flow):
        if self.state(flow) is None or not flow.websocket or not flow.websocket.messages:
            return
        message = flow.websocket.messages[-1]
        state = self.state(flow)
        state['ws_message'] += 1
        # Fragments share a message number; end permits exact reassembly, not
        # treating each packet as a separate provider message.
        fields = {'message': state['ws_message'], 'opcode': int(message.type)}
        direction = 'request' if message.from_client else 'response'
        self.data(flow, 'websocket', direction, message.content, **fields)
        self.emit(flow, 'websocket', direction, 'end', **fields)

    def websocket_end(self, flow):
        self.emit(flow, 'websocket', 'response', 'end')
