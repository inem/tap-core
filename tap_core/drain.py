"""Graceful-off drain: a short-lived loopback CONNECT tunnel (#176).

When `tap off` stops the capture backend, long-lived clients (e.g. the desktop
app running this very session) still hold keep-alive sockets and cached proxy
settings pointing at 127.0.0.1:<port>. A hard stop leaves them hitting a closed
port instead of falling back to direct. This module keeps the port answering for
a bounded window *after* the backend is gone: it tunnels HTTPS `CONNECT` straight
to the origin (no interception, no capture), so straggling clients keep working
while they migrate to the now-direct system routing. The window is bounded and
runs in-process, so it can never outlive `tap off` and collide with a later
`tap on` (which refuses to start on an occupied port).

Scope: `CONNECT` only. Plain-HTTP proxying (absolute-form requests) is not
tunnelled here — every client we care about (Claude, ChatGPT, Cursor, Kimi) is
HTTPS — and a non-CONNECT request gets a clean 501 rather than a silent hang.
Bind is loopback-only, matching the reach the real proxy already had.
"""
import select
import socket
import threading
import time

CONNECT_TIMEOUT = 5.0
IDLE_SLICE = 0.5
BUFFER = 65536


def _read_head(sock, deadline):
    """Read up to the end of the request head (CRLFCRLF). Returns bytes or None."""
    sock.settimeout(IDLE_SLICE)
    data = b""
    while b"\r\n\r\n" not in data:
        if time.monotonic() >= deadline or len(data) > BUFFER:
            return None
        try:
            chunk = sock.recv(BUFFER)
        except socket.timeout:
            continue
        except OSError:
            return None
        if not chunk:
            return None
        data += chunk
    return data


def _connect_target(head):
    """Parse a CONNECT request head into (host, port), or None if not CONNECT."""
    line = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
    parts = line.split(" ")
    if len(parts) < 2 or parts[0].upper() != "CONNECT":
        return None
    authority = parts[1]
    host, _, port = authority.rpartition(":")
    if not host or not port.isdigit():
        return None
    return host, int(port)


def _splice(a, b, deadline):
    """Byte-pump between two sockets until either closes or the deadline passes."""
    peer = {a: b, b: a}
    for sock in (a, b):
        sock.setblocking(False)
    while time.monotonic() < deadline:
        try:
            ready, _, _ = select.select([a, b], [], [], IDLE_SLICE)
        except OSError:
            return
        for src in ready:
            try:
                chunk = src.recv(BUFFER)
            except (BlockingIOError, InterruptedError):
                continue
            except OSError:
                return
            if not chunk:
                return
            try:
                peer[src].sendall(chunk)
            except OSError:
                return


def _handle(client, deadline):
    try:
        head = _read_head(client, deadline)
        target = _connect_target(head) if head else None
        if not target:
            try:
                client.sendall(b"HTTP/1.1 501 Not Implemented\r\nConnection: close\r\n\r\n")
            except OSError:
                pass
            return
        remaining = max(0.0, deadline - time.monotonic())
        try:
            upstream = socket.create_connection(target, timeout=min(CONNECT_TIMEOUT, remaining or CONNECT_TIMEOUT))
        except OSError:
            try:
                client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
            except OSError:
                pass
            return
        try:
            client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            _splice(client, upstream, deadline)
        finally:
            upstream.close()
    finally:
        client.close()


class DrainProxy:
    """A bounded, loopback-only CONNECT tunnel. Bind on construct, serve a window."""

    def __init__(self, host="127.0.0.1", port=0):
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind((host, port))
        self.listener.listen(128)
        self.port = self.listener.getsockname()[1]
        self._threads = []

    def serve(self, seconds):
        deadline = time.monotonic() + seconds
        self.listener.settimeout(IDLE_SLICE)
        while time.monotonic() < deadline:
            try:
                client, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            worker = threading.Thread(target=_handle, args=(client, deadline), daemon=True)
            worker.start()
            self._threads.append(worker)

    def close(self):
        try:
            self.listener.close()
        except OSError:
            pass


def run(host, port, seconds):
    """Hold `host:port` as a CONNECT tunnel for `seconds`. Best-effort.

    Returns True if the window ran, False if it was disabled (seconds <= 0) or the
    port could not be bound (never fatal to `tap off`; the backend is already gone).
    """
    if seconds <= 0:
        return False
    try:
        proxy = DrainProxy(host, port)
    except OSError:
        return False
    try:
        proxy.serve(seconds)
    finally:
        proxy.close()
    return True
