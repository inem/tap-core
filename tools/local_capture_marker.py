"""Fixture-only marker for the opt-in Local Capture acceptance procedure.

Never use this addon with real traffic. It answers only a reserved loopback path
and prints no request bodies, headers, process lists, or URLs. The response proves
the request reached this backend; absence alone does not prove passthrough.
"""
from mitmproxy import http


def request(flow):
    if flow.request.host == "127.0.0.1" and flow.request.path == "/tap-core-routing-fixture":
        flow.response = http.Response.make(
            200, b"tap-core-intercepted\n", {"Content-Type": "text/plain"})
