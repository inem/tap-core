"""Loopback TLS is tunnelled untouched; everything else keeps interception."""
import re
import unittest

from tap_core.passthrough import LOOPBACK_TLS_PATTERNS
from tap_core.routing import ExplicitProxyRouting


class LoopbackTlsPassthroughTests(unittest.TestCase):
    def matches(self, host_port):
        return any(re.search(pattern, host_port) for pattern in LOOPBACK_TLS_PATTERNS)

    def test_loopback_hosts_only(self):
        for value in ('127.0.0.1:58361', '127.8.9.10:443', 'localhost:443', 'app.localhost:8443',
                      '[::1]:9000', '::1:9000'):
            self.assertTrue(self.matches(value), value)
        for value in ('example.com:443', 'localhost.example.com:443', '10.0.0.1:443',
                      '1270.0.0.1:1', 'my127.0.0.1.example:443'):
            self.assertFalse(self.matches(value), value)

    def test_backend_ignores_loopback_tls_under_every_routing(self):
        class Profile:
            port, passthrough, routing = 18999, ['*.apple.com'], 'explicit'
        args = ExplicitProxyRouting(Profile(), None).backend_args()
        ignored = [args[i + 1] for i, value in enumerate(args) if value == '--ignore-hosts']
        for pattern in LOOPBACK_TLS_PATTERNS:
            self.assertIn(pattern, ignored)
        self.assertIn(r'^(.+\.)?apple\.com:\d+$', ignored)  # declared passthrough kept


if __name__ == '__main__':
    unittest.main()
