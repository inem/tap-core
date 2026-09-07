"""Local socket fixtures for the opt-in live harness; no external services."""
import errno
import socket
import unittest

from tools.check_live_slice import reserve_port


class PortReservationTests(unittest.TestCase):
    def assert_reserved(self, address):
        with socket.socket() as contender:
            with self.assertRaises(OSError) as failure:
                contender.bind(address)
            self.assertEqual(failure.exception.errno, errno.EADDRINUSE)

    def assert_released(self, address):
        with socket.socket() as service:
            service.bind(address)
            service.listen()

    def test_simultaneous_reservations_are_distinct_and_exclusive(self):
        with reserve_port() as hub, reserve_port() as proxy:
            self.assertNotEqual(hub.getsockname(), proxy.getsockname())
            for listener in (hub, proxy):
                self.assertEqual(listener.getsockname()[0], '127.0.0.1')
                self.assert_reserved(listener.getsockname())
                with socket.create_connection(listener.getsockname(), timeout=1), listener.accept()[0]:
                    pass

    def test_service_handoff_keeps_other_reservation_in_either_startup_order(self):
        for first in (0, 1):
            with self.subTest(first=first):
                with reserve_port() as hub, reserve_port() as proxy:
                    listeners = (hub, proxy)
                    addresses = [listener.getsockname() for listener in listeners]
                    listeners[first].close()
                    with socket.socket() as service:
                        service.bind(addresses[first])
                        service.listen()
                        self.assert_reserved(addresses[1 - first])
                        listeners[1 - first].close()
                        self.assert_released(addresses[1 - first])
                self.assert_released(addresses[first])

    def test_context_exit_releases_all_reservations_after_failure(self):
        with self.assertRaisesRegex(RuntimeError, 'fixture failed'):
            with reserve_port() as hub, reserve_port() as proxy:
                addresses = [hub.getsockname(), proxy.getsockname()]
                raise RuntimeError('fixture failed')
        for address in addresses:
            self.assert_released(address)

    def test_context_exit_closes_pending_reservation_after_early_release(self):
        with self.assertRaisesRegex(RuntimeError, 'startup failed'):
            with reserve_port() as hub, reserve_port() as proxy:
                addresses = [hub.getsockname(), proxy.getsockname()]
                hub.close()
                raise RuntimeError('startup failed')
        for address in addresses:
            self.assert_released(address)


if __name__ == '__main__':
    unittest.main()
