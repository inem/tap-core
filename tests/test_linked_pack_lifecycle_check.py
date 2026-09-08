"""Gate unit tests for linked-http lifecycle check claims."""
import unittest

from tools.check_linked_pack_lifecycle import scenario_failures


def passing_steps():
    return {
        'handler_timeout': True,
        'reader_error_visible': True,
        'incompatible_update_refused': True,
        'selected_unchanged': True,
        'checkpoint_unchanged': True,
        'uninstall_removed_code': True,
        'data_retained': True,
    }


class LinkedLifecycleGateTests(unittest.TestCase):
    def test_passing_has_no_failures(self):
        self.assertEqual(scenario_failures(passing_steps()), [])

    def test_missing_timeout_fails(self):
        steps = passing_steps()
        steps['handler_timeout'] = False
        self.assertIn('handler_timeout', scenario_failures(steps))

    def test_lost_data_fails(self):
        steps = passing_steps()
        steps['data_retained'] = False
        self.assertIn('data_retained', scenario_failures(steps))


if __name__ == '__main__':
    unittest.main()
