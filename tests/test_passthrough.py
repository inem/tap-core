import json
import re
import tempfile
import unittest
from pathlib import Path

from tap_core.passthrough import DEFAULT_PASSTHROUGH, configuration, ignore_host_patterns
from tap_core.routing import select_routing
from tap_core.runtime import Profile, TapError


class PassthroughTests(unittest.TestCase):
    def test_default_when_absent_and_explicit_lists_validate_and_deduplicate(self):
        self.assertEqual(configuration(None), DEFAULT_PASSTHROUGH)
        self.assertEqual(configuration(["*.icloud.com", "gateway.icloud.com", "*.icloud.com", "17.0.0.0/8"]),
                         ["*.icloud.com", "gateway.icloud.com", "17.0.0.0/8"])
        self.assertEqual(configuration([]), [])
        for bad in ("not a list", [""], ["*"], ["icloud"], ["*.icloud.com "], ["a b.com"], [42], ["*.*.com"]):
            with self.assertRaises(ValueError, msg=bad):
                configuration(bad)
        with self.assertRaises(ValueError):
            configuration([f"h{i}.example" for i in range(65)])

    def test_ignore_host_patterns_match_the_connect_host_and_skip_cidr(self):
        patterns = ignore_host_patterns(["*.icloud.com", "humb.apple.com", "17.0.0.0/8"])
        self.assertEqual(len(patterns), 2)
        wildcard, exact = (re.compile(p) for p in patterns)
        for host in ("gateway.icloud.com:443", "p173-caldav.icloud.com:443", "icloud.com:8443"):
            self.assertTrue(wildcard.search(host), host)
        for host in ("evil-icloud.com:443", "icloud.com.attacker.net:443", "gateway.icloud.comx:443"):
            self.assertFalse(wildcard.search(host), host)
        self.assertTrue(exact.search("humb.apple.com:443"))
        self.assertFalse(exact.search("www.humb.apple.com:443"))

    def test_profile_persists_the_list_and_both_routings_pass_it_to_the_backend(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            profile = Profile(root, "/fixture/mitmdump", 19070, "explicit", "http://example.test", [])
            self.assertEqual(profile.passthrough, DEFAULT_PASSTHROUGH)
            profile.save()
            saved = json.loads((root / "profile.json").read_text())
            self.assertEqual(saved["passthrough"], DEFAULT_PASSTHROUGH)
            args = select_routing(Profile.load(root), None).backend_args()
            self.assertEqual(args.count("--ignore-hosts"), len(DEFAULT_PASSTHROUGH) - 1)
            self.assertIn(r"^(.+\.)?icloud\.com:\d+$", args)
            # Older profile.json without the key gets the default; an explicit empty list captures everything.
            del saved["passthrough"]
            (root / "profile.json").write_text(json.dumps(saved))
            self.assertEqual(Profile.load(root).passthrough, DEFAULT_PASSTHROUGH)
            saved["passthrough"] = []
            (root / "profile.json").write_text(json.dumps(saved))
            self.assertNotIn("--ignore-hosts", select_routing(Profile.load(root), None).backend_args())
            saved["passthrough"] = ["bad entry"]
            (root / "profile.json").write_text(json.dumps(saved))
            with self.assertRaises(TapError):
                Profile.load(root)


if __name__ == "__main__":
    unittest.main()
