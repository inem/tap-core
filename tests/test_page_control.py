import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from tap_core.cli import parser
from tap_core import page_control
from tap_core.page_control import allow, call
from tap_core.runtime import Profile, TapError


def bridge(**overrides):
    value = {"version": 1, "enabled": True, "hub_port": 19002,
             "allow_origins": [], "exclude_origins": [], "page_scripts": []}
    value.update(overrides)
    return value


class DevelopmentOriginTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.profile = Profile(self.root, "/fixture/mitmdump", 18999, "explicit",
                               "https://example.test", [], bridge=bridge())
        self.profile.save()

    def tearDown(self):
        self.temporary.cleanup()

    def test_allow_persists_exact_origin_and_is_idempotent(self):
        result = allow(self.profile, "https://www.google.com")
        self.assertTrue(result["changed"])
        saved = json.loads((self.root / "profile.json").read_text())
        self.assertEqual(saved["bridge"]["allow_origins"], ["https://www.google.com"])
        development = json.loads((self.root / "state/development.json").read_text())
        self.assertEqual(development, {"version": 1, "origins": ["https://www.google.com"], "tools": []})
        self.assertEqual(result["mode"], "development")
        self.assertFalse(allow(self.profile, "https://www.google.com")["changed"])

    def test_cli_accepts_allow_origin(self):
        args = parser().parse_args([
            "--profile", str(self.root),
            "dev", "allow", "https://example.test",
        ])
        self.assertEqual((args.command, args.dev_action, args.origin),
                         ("dev", "allow", "https://example.test"))

    def test_explicit_dev_allow_removes_matching_exclusion(self):
        self.profile.bridge = bridge(exclude_origins=["https://example.test"])
        self.profile.save()
        self.assertTrue(allow(self.profile, "https://example.test")["allowed"])
        self.assertEqual(self.profile.bridge["exclude_origins"], [])

    def test_allow_rejects_non_origin_and_disabled_bridge(self):
        with self.assertRaisesRegex(TapError, "exact canonical"):
            allow(self.profile, "https://example.test/path")
        self.profile.bridge["enabled"] = False
        with self.assertRaisesRegex(TapError, "enabled bridge"):
            allow(self.profile, "https://example.test")

    def _http_error(self, payload):
        body = json.dumps(payload).encode()
        return HTTPError("http://127.0.0.1:19002/v1/pages/p/commands", 400,
                         "Bad Request", {}, io.BytesIO(body))

    def test_failed_page_command_surfaces_the_page_side_message(self):
        # A Trusted Types refusal (or any page-side throw) must reach the CLI, not
        # be flattened to a bare error code.
        error = self._http_error({"ok": False, "error": {
            "code": "operation_failed",
            "message": "This document requires 'TrustedScript' assignment."}})
        with patch.object(page_control, "_runtime", return_value=("http://127.0.0.1:19002", "tok")), \
                patch.object(page_control, "urlopen", side_effect=error):
            with self.assertRaises(TapError) as caught:
                call(self.root, "page-id", "tap.dev.execute", {"source": "return 1"})
        text = str(caught.exception)
        self.assertIn("operation_failed", text)
        self.assertIn("TrustedScript", text)

    def test_failed_page_command_without_message_uses_the_code(self):
        error = self._http_error({"ok": False, "error": {"code": "page_not_found"}})
        with patch.object(page_control, "_runtime", return_value=("http://127.0.0.1:19002", "tok")), \
                patch.object(page_control, "urlopen", side_effect=error):
            with self.assertRaisesRegex(TapError, "Page command failed: page_not_found"):
                call(self.root, "page-id", "tap.dev.inspect", {"selector": "main", "limit": 1})


if __name__ == "__main__":
    unittest.main()
