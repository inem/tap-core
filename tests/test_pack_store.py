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

    def sibling_source(self, pack_id="example.youtube-second", *, shared_version="0.1.0",
                       shared_suffix="", origins=None):
        source = self.root / pack_id
        shutil.copytree(SOURCE, source)
        manifest = json.loads((source / "pack.json").read_text())
        manifest["id"] = pack_id
        manifest["files"].append("second.js")
        manifest["entrypoints"]["page"]["scripts"] = [
            {"id": "youtube.ui", "version": shared_version, "file": "youtube-ui.js"},
            {"id": pack_id + ".feature", "version": "0.1.0", "file": "second.js"},
        ]
        if origins is not None:
            manifest["access"]["origins"] = origins
        (source / "pack.json").write_text(json.dumps(manifest, indent=2) + "\n")
        (source / "second.js").write_text("window.secondPack = true;\n")
        if shared_suffix:
            with (source / "youtube-ui.js").open("a") as handle:
                handle.write("\n// " + shared_suffix + "\n")
        return source

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

    def test_same_origin_packs_merge_declarations_and_inject_shared_resource_once(self):
        first = self.artifact(SOURCE, "first.tap-pack")
        second = self.artifact(self.sibling_source(), "second.tap-pack")
        self.store.install(first)
        self.store.install(second)
        self.store.enable("example.youtube-copy-links", "0.1.0",
                          origins=ORIGINS, capabilities=CAPABILITIES)
        self.store.enable("example.youtube-second", "0.1.0",
                          origins=ORIGINS, capabilities=CAPABILITIES)

        effective = self.store.effective_bridge(bridge())
        names = [Path(path).name for path in effective["page_scripts"]]
        self.assertEqual(names, ["youtube-ui.js", "copy-links.js", "second.js"])
        self.assertEqual(names.count("youtube-ui.js"), 1)
        self.assertEqual(effective["page_script_origins"], [ORIGINS, ORIGINS, ORIGINS])
        self.assertEqual(effective_configuration(self.profile, bridge()), effective)

    def test_shared_resource_version_or_content_conflict_fails_before_activation(self):
        first = self.artifact(SOURCE, "first.tap-pack")
        wrong_version = self.artifact(
            self.sibling_source("example.version-conflict", shared_version="0.2.0"),
            "wrong-version.tap-pack")
        wrong_content = self.artifact(
            self.sibling_source("example.content-conflict", shared_suffix="different bytes"),
            "wrong-content.tap-pack")
        for artifact in (first, wrong_version, wrong_content):
            self.store.install(artifact)
        self.store.enable("example.youtube-copy-links", "0.1.0",
                          origins=ORIGINS, capabilities=CAPABILITIES)
        with self.assertRaisesRegex(PackError, "conflicting versions"):
            self.store.enable("example.version-conflict", "0.1.0",
                              origins=ORIGINS, capabilities=CAPABILITIES)
        with self.assertRaisesRegex(PackError, "conflicting content"):
            self.store.enable("example.content-conflict", "0.1.0",
                              origins=ORIGINS, capabilities=CAPABILITIES)
        registry = self.store.status()["packs"]
        self.assertFalse(registry["example.version-conflict"]["enabled"])
        self.assertFalse(registry["example.content-conflict"]["enabled"])

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
