import hashlib
import io
import json
from pathlib import Path
import shutil
import tarfile
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from tap_core import cli
from tap_core.bridge import effective_configuration
from tap_core.pack_store import PackStore, build_artifact
from tap_core.packs import PackError
from tap_core.runtime import Profile


SOURCE = Path(__file__).resolve().parent.parent / "examples/youtube-copy-links"
ORIGINS = ["https://www.youtube.com", "https://youtube.com"]
CAPABILITIES = ["page.inject"]


def bridge():
    return {"version": 1, "enabled": True, "hub_port": 19002,
            "allow_origins": [], "exclude_origins": [], "page_scripts": []}


class PackStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="tap-pack-store-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.profile = self.root / "profile"
        (self.profile / "state").mkdir(parents=True)
        (self.profile / "profile.json").write_text(json.dumps({"bridge": bridge()}) + "\n")
        self.store = PackStore(self.profile)

    def source_version(self, version, suffix=""):
        source = self.root / ("source-" + version)
        shutil.copytree(SOURCE, source)
        manifest = json.loads((source / "pack.json").read_text())
        manifest["version"] = version
        (source / "pack.json").write_text(json.dumps(manifest, indent=2) + "\n")
        if suffix:
            with (source / "copy-links.js").open("a") as handle:
                handle.write("\n// " + suffix + "\n")
        return source

    def artifact(self, source, name):
        output = self.root / name
        build_artifact(source, output)
        return output

    def test_artifact_is_reproducible_and_installed_binding_uses_snapshot(self):
        first = self.artifact(SOURCE, "first.tap-pack")
        second = self.artifact(SOURCE, "second.tap-pack")
        self.assertEqual(hashlib.sha256(first.read_bytes()).digest(),
                         hashlib.sha256(second.read_bytes()).digest())

        installed = self.store.install(first)
        self.assertFalse(installed["enabled"])
        with self.assertRaisesRegex(PackError, "not granted"):
            self.store.enable("example.youtube-copy-links", "0.1.0")

        result = self.store.enable("example.youtube-copy-links", "0.1.0",
                                   origins=ORIGINS, capabilities=CAPABILITIES)
        self.assertTrue(result["enabled"])
        effective = self.store.effective_bridge(bridge())
        self.assertEqual(effective_configuration(self.profile, bridge()), effective)
        self.assertEqual(effective["allow_origins"], ORIGINS)
        self.assertEqual([Path(path).name for path in effective["page_scripts"]],
                         ["youtube-ui.js", "copy-links.js"])
        self.assertTrue(all(str(self.profile / "packs") in path
                            for path in effective["page_scripts"]))
        self.assertFalse((self.profile / "state/packs/example.youtube-copy-links").exists())

        Path(effective["page_scripts"][1]).write_text("tampered")
        with self.assertRaisesRegex(PackError, "integrity check failed"):
            self.store.effective_bridge(bridge())

    def test_update_rollback_disable_and_uninstall_preserve_owned_data(self):
        first = self.artifact(SOURCE, "v1.tap-pack")
        second = self.artifact(self.source_version("0.2.0", "version two"), "v2.tap-pack")
        self.store.install(first)
        self.store.enable("example.youtube-copy-links", "0.1.0",
                          origins=ORIGINS, capabilities=CAPABILITIES)
        self.assertEqual(self.store.update(second)["version"], "0.2.0")
        self.assertEqual(self.store.status()["packs"]["example.youtube-copy-links"]["selected"],
                         "0.2.0")
        self.assertEqual(self.store.rollback("example.youtube-copy-links")["version"], "0.1.0")

        for parent in ("state", "data", "logs"):
            owned = self.profile / parent / "packs/example.youtube-copy-links"
            owned.mkdir(parents=True)
            (owned / "keep").write_text("retained")
        self.store.disable("example.youtube-copy-links")
        removed = self.store.uninstall("example.youtube-copy-links")
        self.assertTrue(removed["state_retained"])
        self.assertFalse((self.profile / "packs/example.youtube-copy-links/versions/0.1.0").exists())
        self.assertFalse((self.profile / "packs/example.youtube-copy-links/versions/0.2.0").exists())
        for parent in ("state", "data", "logs"):
            self.assertEqual((self.profile / parent / "packs/example.youtube-copy-links/keep").read_text(),
                             "retained")

    def test_failed_update_installs_but_does_not_activate_new_version(self):
        first = self.artifact(SOURCE, "v1.tap-pack")
        source = self.source_version("0.2.0", "requests another origin")
        manifest = json.loads((source / "pack.json").read_text())
        manifest["access"]["origins"].append("https://music.youtube.com")
        (source / "pack.json").write_text(json.dumps(manifest, indent=2) + "\n")
        second = self.artifact(source, "v2.tap-pack")
        self.store.install(first)
        self.store.enable("example.youtube-copy-links", "0.1.0",
                          origins=ORIGINS, capabilities=CAPABILITIES)
        with self.assertRaisesRegex(PackError, "not granted"):
            self.store.update(second)
        record = self.store.status()["packs"]["example.youtube-copy-links"]
        self.assertEqual(record["selected"], "0.1.0")
        self.assertTrue(record["enabled"])
        self.assertIn("0.2.0", record["versions"])

    def test_archive_traversal_is_rejected_before_install(self):
        artifact = self.root / "bad.tap-pack"
        with tarfile.open(artifact, "w:gz") as archive:
            info = tarfile.TarInfo("../outside")
            info.size = 1
            archive.addfile(info, io.BytesIO(b"x"))
        with self.assertRaisesRegex(PackError, "unsafe"):
            self.store.install(artifact)
        self.assertFalse((self.root / "outside").exists())

    def test_profile_cli_installs_grants_and_explains_effective_origin(self):
        profile = Profile(self.profile, "/fixture/mitmdump", 19001, "explicit",
                          "http://example.test", [], bridge=bridge())
        profile.save()
        artifact = self.artifact(SOURCE, "cli.tap-pack")
        base = ["--profile", str(self.profile), "pack"]
        output = io.StringIO()
        with patch("tap_core.cli.platform.system", return_value="Darwin"), \
                patch("tap_core.cli.MacOS.service_loaded", return_value=False), \
                redirect_stdout(output):
            self.assertEqual(cli.main([*base, "install", str(artifact)]), 0)
            self.assertEqual(cli.main([*base, "enable", "example.youtube-copy-links",
                                       "--version", "0.1.0",
                                       "--grant-origin", "https://www.youtube.com",
                                       "--grant-origin", "https://youtube.com",
                                       "--grant-capability", "page.inject"]), 0)
            self.assertEqual(cli.main(["--profile", str(self.profile), "bridge", "explain",
                                       "--origin", "https://www.youtube.com"]), 0)
        self.assertIn('"allowed": true', output.getvalue())
        installed = self.store.effective_bridge(bridge())["page_scripts"]
        self.assertTrue(all(str(self.profile / "packs") in path for path in installed))


if __name__ == "__main__":
    unittest.main()
