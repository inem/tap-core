"""Gate unit tests for linked-http lifecycle check claims."""
import unittest
import io
from pathlib import Path
import tarfile
import tempfile

from tools.check_linked_pack_lifecycle import scenario_failures, extract_artifact
from tap_core.packs import PackError


def passing_steps():
    return {
        'update_rollback_before_progress': True,
        'incompatible_rollback_refused': True,
        'bindings_removed': True,
        'handler_timeout': True,
        'reader_error_visible': True,
        'incompatible_update_refused': True,
        'selected_unchanged': True,
        'checkpoint_unchanged': True,
        'uninstall_removed_code': True,
        'data_retained': True,
    }


class LinkedLifecycleGateTests(unittest.TestCase):
    def test_downloaded_archive_cannot_escape_or_create_links(self):
        for name, kind in (('../escaped', tarfile.REGTYPE), ('link', tarfile.SYMTYPE)):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                artifact = root / 'unsafe.tap-pack'
                with tarfile.open(artifact, 'w:gz') as archive:
                    entry = tarfile.TarInfo(name)
                    entry.type = kind
                    entry.linkname = '../escaped' if kind == tarfile.SYMTYPE else ''
                    entry.size = 0
                    archive.addfile(entry, io.BytesIO())
                with self.assertRaises(PackError):
                    extract_artifact(artifact, root / 'extracted')
                self.assertFalse((root / 'escaped').exists())
                self.assertFalse((root / 'extracted/link').is_symlink())

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
