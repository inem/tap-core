import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from tap_core.page_resources import ResourceError, load_resource


FIXTURE = (Path(__file__).resolve().parent.parent
           / "contracts/page-resource/v1/provider.fixture")


class PageResourceContractTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="tap-page-resource-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / "provider"
        shutil.copytree(FIXTURE, self.root)

    def test_reference_provider_and_cli_conform(self):
        value = load_resource(self.root)
        self.assertEqual(value["contract"], "tap.page-resource/v1")
        result = subprocess.run([sys.executable, "-B", "-m", "tap_core.page_resources",
                                 str(self.root)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["valid"])

    def test_changed_bytes_and_unsafe_file_fail(self):
        (self.root / "shared-ui.js").write_text("changed\n")
        with self.assertRaisesRegex(ResourceError, "do not match"):
            load_resource(self.root)
        value = json.loads((self.root / "tap-resource.json").read_text())
        value["file"] = "../outside.js"
        (self.root / "tap-resource.json").write_text(json.dumps(value))
        with self.assertRaisesRegex(ResourceError, "unsafe"):
            load_resource(self.root)

    def test_unknown_fields_and_duplicate_json_keys_fail(self):
        value = json.loads((self.root / "tap-resource.json").read_text())
        value["download"] = "https://example.test/latest.js"
        (self.root / "tap-resource.json").write_text(json.dumps(value))
        with self.assertRaisesRegex(ResourceError, "exactly"):
            load_resource(self.root)
        (self.root / "tap-resource.json").write_text(
            '{"contract":"tap.page-resource/v1","contract":"other"}')
        with self.assertRaisesRegex(ResourceError, "duplicate key"):
            load_resource(self.root)

    def test_malformed_kind_is_a_contract_error(self):
        value = json.loads((self.root / "tap-resource.json").read_text())
        value["kind"] = ["browser-classic-script"]
        (self.root / "tap-resource.json").write_text(json.dumps(value))
        with self.assertRaisesRegex(ResourceError, "kind is unsupported"):
            load_resource(self.root)


if __name__ == "__main__":
    unittest.main()
