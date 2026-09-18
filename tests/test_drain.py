"""The graceful-off CONNECT drain tunnel (#176), exercised on loopback ports."""
import socket
import threading
import time
import unittest

from tap_core.drain import DrainProxy, run


class EchoServer:
    """Minimal loopback TCP echo target the drain can tunnel to."""
    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        self.sock.settimeout(0.5)
        while True:
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._echo, args=(conn,), daemon=True).start()

    def _echo(self, conn):
        with conn:
            while True:
                try:
                    chunk = conn.recv(4096)
                except OSError:
                    return
                if not chunk:
                    return
                conn.sendall(chunk)

    def close(self):
        self.sock.close()


class DrainTests(unittest.TestCase):
    def setUp(self):
        self.echo = EchoServer()
        self.addCleanup(self.echo.close)
        self.proxy = DrainProxy(port=0)
        self.addCleanup(self.proxy.close)
        self.server = threading.Thread(target=self.proxy.serve, args=(5,), daemon=True)
        self.server.start()

    def _client(self):
        client = socket.create_connection(("127.0.0.1", self.proxy.port), timeout=3)
        client.settimeout(3)
        self.addCleanup(client.close)
        return client

    def _read_status(self, client):
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = client.recv(4096)
            if not chunk:
                break
            data += chunk
        return data

    def test_connect_tunnels_bytes_to_the_origin(self):
        client = self._client()
        client.sendall(f"CONNECT 127.0.0.1:{self.echo.port} HTTP/1.1\r\n\r\n".encode())
        status = self._read_status(client)
        self.assertIn(b"200 Connection established", status)
        client.sendall(b"ping through the tunnel")
        self.assertEqual(client.recv(4096), b"ping through the tunnel")

    def test_non_connect_request_gets_501(self):
        client = self._client()
        client.sendall(b"GET http://example.test/ HTTP/1.1\r\nHost: example.test\r\n\r\n")
        status = self._read_status(client)
        self.assertIn(b"501 Not Implemented", status)

    def test_connect_to_dead_origin_reports_502(self):
        # A port nothing listens on: the drain must answer, not hang the client.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            dead = probe.getsockname()[1]
        client = self._client()
        client.sendall(f"CONNECT 127.0.0.1:{dead} HTTP/1.1\r\n\r\n".encode())
        status = self._read_status(client)
        self.assertIn(b"502 Bad Gateway", status)


class DrainWindowTests(unittest.TestCase):
    def test_run_disabled_when_window_is_not_positive(self):
        self.assertFalse(run("127.0.0.1", 0, 0))

    def test_run_binds_and_honors_the_window(self):
        start = time.monotonic()
        self.assertTrue(run("127.0.0.1", 0, 0.3))
        self.assertGreaterEqual(time.monotonic() - start, 0.25)

    def test_serve_returns_when_the_listener_is_closed(self):
        proxy = DrainProxy(port=0)
        server = threading.Thread(target=proxy.serve, args=(30,), daemon=True)
        server.start()
        proxy.close()
        server.join(timeout=2)
        self.assertFalse(server.is_alive())


if __name__ == "__main__":
    unittest.main()
