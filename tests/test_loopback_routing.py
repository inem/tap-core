import copy
import tempfile
import unittest
from pathlib import Path
from tap_core.runtime import Profile
from tap_core.routing import SystemProxyRouting, ExplicitProxyRouting
from tests.test_runtime import NetworkFixture

class LoopbackRoutingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        (root / 'state').mkdir()
        self.profile = Profile(root, '/fixture/mitmdump', 18999, 'system', 'http://example.test', [])
        self.os = NetworkFixture()
        self.before = copy.deepcopy(self.os.network)
        self.route = SystemProxyRouting(self.profile, self.os)

    def test_old_route_reconciles_and_off_restores_original_bypass(self):
        self.route.enable()
        original = self.profile.snapshot.read_bytes()
        for service in self.os.services():
            self.os.network[service]['bypass'] = list(self.profile.passthrough)
        self.assertFalse(self.route.bypasses_match(self.before))
        self.route.enable()
        for service in self.os.services():
            self.assertEqual(self.os.bypass(service), ['<-loopback>', *self.profile.passthrough])
        self.assertEqual(self.profile.snapshot.read_bytes(), original)
        self.route.enable()
        self.assertEqual(self.profile.snapshot.read_bytes(), original)
        self.route.restore()
        for service in self.os.services():
            self.assertEqual(self.os.bypass(service), self.before[service]['bypass'])
            self.assertFalse(self.os.proxy(service)['enabled'])
            self.assertFalse(self.os.proxy(service, True)['enabled'])

    def test_foreign_bypass_is_not_reconciled_as_known_old_route(self):
        self.route.enable()
        self.os.network['Wi-Fi']['bypass'] += ['foreign.example']
        self.assertFalse(self.route.bypasses_are_tightenable(self.before))

    def test_marker_does_not_become_backend_tls_passthrough(self):
        for adapter in (self.route, ExplicitProxyRouting(self.profile, self.os)):
            self.assertNotIn('<-loopback>', ' '.join(adapter.backend_args()))
        self.assertNotIn('<-loopback>', self.profile.passthrough)
