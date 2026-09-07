#!/usr/bin/env python3
"""Controlled macOS TCP ownership experiment. Does not route traffic.

Only loopback sockets and processes created by this program are inspected.
No system proxy, certificates, capture service, or Network Extension is changed.
Results contain executable basenames and boolean identity checks, not PIDs/paths.
"""
import argparse
import ctypes
import json
import os
from pathlib import Path
import platform
import queue
import socket
import statistics
import subprocess
import sys
import threading
import time


def executable(pid):
    libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    buf = ctypes.create_string_buffer(4096)
    size = libproc.proc_pidpath(pid, buf, len(buf))
    return Path(os.fsdecode(buf.value)).name if size > 0 else None


def socket_owners(pids):
    """Bound lsof scope at invocation; never gather unrelated process rows."""
    started = time.perf_counter()
    result = subprocess.run(
        ["/usr/sbin/lsof", "-nP", "-a", "-p", ",".join(map(str, pids)),
         "-iTCP", "-Fpcn"], capture_output=True, text=True, timeout=10)
    if result.returncode not in (0, 1) or result.stderr:
        raise RuntimeError("lsof inspection failed; result is not 'no owner'")
    rows, pid, command = [], None, None
    for line in result.stdout.splitlines():
        if line.startswith("p"):
            pid = int(line[1:])
        elif line.startswith("c"):
            command = line[1:]
        elif line.startswith("n") and "->" in line:
            rows.append((pid, command, line[1:]))
    return rows, (time.perf_counter() - started) * 1000


class Origin:
    def __init__(self):
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen()
        self.listener.settimeout(0.2)
        self.port = self.listener.getsockname()[1]
        self.accepted = queue.Queue()
        self.release_events = []
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        while not self.stopping.is_set():
            try:
                conn, peer = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            release = threading.Event()
            self.release_events.append(release)
            threading.Thread(target=self.hold, args=(conn, peer, release), daemon=True).start()

    def hold(self, conn, peer, release):
        with conn:
            conn.settimeout(10)
            request = b""
            while b"\r\n\r\n" not in request and len(request) < 4096:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                request += chunk
            label = request.split(b" ")[1].decode("ascii").lstrip("/")
            self.accepted.put((label, peer[1], release))
            release.wait(30)
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")

    def close(self):
        self.stopping.set()
        for event in self.release_events:
            event.set()
        self.listener.close()
        self.thread.join(timeout=1)


def python_client(port, label, source_port):
    with socket.socket() as conn:
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        conn.settimeout(30)
        conn.bind(("127.0.0.1", source_port))
        conn.connect(("127.0.0.1", port))
        conn.sendall(f"GET /{label} HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n".encode())
        while conn.recv(4096):
            pass


def run():
    if sys.platform != "darwin":
        raise SystemExit("This experiment targets macOS only")
    origin, children = Origin(), []
    timings, observations = [], {}

    def start(label, kind="python", source_port=0):
        if kind == "curl":
            command = ["/usr/bin/curl", "--noproxy", "*", "--silent", "--show-error",
                       "--max-time", "30", "--output", os.devnull,
                       f"http://127.0.0.1:{origin.port}/{label}"]
        else:
            command = [sys.executable, __file__, "--client", str(origin.port),
                       label, str(source_port)]
        child = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        children.append(child)
        got_label, port, release = origin.accepted.get(timeout=5)
        assert got_label == label
        observations[label] = (child, port, release)
        return child, port, release

    def matches(rows, pid, port):
        connection = f"127.0.0.1:{port}->127.0.0.1:{origin.port}"
        return any(row[0] == pid and row[2] == connection for row in rows)

    try:
        a, a_port, a_release = start("python")
        b, b_port, b_release = start("curl", "curl")
        passes = []
        for _ in range(10):
            rows, elapsed = socket_owners([a.pid, b.pid])
            timings.append(elapsed)
            passes.append(matches(rows, a.pid, a_port) and matches(rows, b.pid, b_port))
        parent_rows, _ = socket_owners([os.getpid()])
        parent_lookup_does_not_find_clients = not any(
            matches(parent_rows, os.getpid(), port) for port in (a_port, b_port))
        names = {"python": executable(a.pid), "curl": executable(b.pid)}

        b_release.set()
        assert b.wait(timeout=5) == 0
        old_rows, _ = socket_owners([b.pid])
        b2, b2_port, b2_release = start("curl-restarted", "curl")
        rows, _ = socket_owners([b2.pid])
        restart = {"pid_changed": b2.pid != b.pid,
                   "new_connection_attributed": matches(rows, b2.pid, b2_port),
                   "old_connection_absent_after_exit": not matches(old_rows, b.pid, b_port),
                   "executable_basename_unchanged": executable(b2.pid) == names["curl"]}

        # Server closes first, avoiding TIME_WAIT on the client port.
        a_release.set()
        assert a.wait(timeout=5) == 0
        a2, a2_port, a2_release = start("reused-source-port", source_port=a_port)
        rows, _ = socket_owners([a.pid, a2.pid])
        port_reuse = {"same_connection_tuple": a2_port == a_port,
                      "pid_changed": a2.pid != a.pid,
                      "new_owner_attributed": matches(rows, a2.pid, a2_port),
                      "old_pid_no_longer_owns_tuple": not matches(rows, a.pid, a_port)}
        a2_release.set()
        b2_release.set()
        assert a2.wait(timeout=5) == b2.wait(timeout=5) == 0
        missing_rows, _ = socket_owners([a2.pid])

        result = {"platform": {"system": platform.system(), "release": platform.mac_ver()[0],
                               "machine": platform.machine()},
                  "scope": "self-created processes, IPv4 loopback TCP, same origin",
                  "attribution": {"status": "verified" if all(passes) else "failed",
                                  "samples": len(passes), "samples_matching_both_clients": sum(passes),
                                  "executables": names},
                  "parent_pid_does_not_own_child_connections": parent_lookup_does_not_find_clients,
                  "restart": restart, "source_port_reuse": port_reuse,
                  "closed_connection_lookup": "unknown" if not missing_rows else "unexpected owner",
                  "lsof_wall_ms": {"min": round(min(timings), 2),
                                   "median": round(statistics.median(timings), 2),
                                   "max": round(max(timings), 2), "samples": len(timings)},
                  "routing": {"status": "unresolved", "reason": "attribution-only experiment"},
                  "pid_reuse": "unresolved; not forced", "bundle_identity": "unresolved",
                  "system_settings_changed": False}
        print(json.dumps(result, indent=2, sort_keys=True))
        assert all(passes) and all(restart.values()) and all(port_reuse.values())
        assert parent_lookup_does_not_find_clients and not missing_rows
    finally:
        origin.close()
        for child in children:
            if child.poll() is None:
                child.terminate()
            child.communicate(timeout=5)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--client":
        python_client(int(sys.argv[2]), sys.argv[3], int(sys.argv[4]))
    else:
        argparse.ArgumentParser(description=__doc__).parse_args()
        run()
