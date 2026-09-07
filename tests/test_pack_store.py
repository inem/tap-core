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


SOURCE = Path(__file__).resolve().parent.parent / "fixtures/packs/installed-page"
PACK_ID = "fixture.installed-page"
ORIGINS = ["https://fixture.example"]
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
        if suffix:
            with (source / "feature.js").open("a") as handle:
                handle.write("\n// " + suffix + "\n")
        feature = next(resource for resource in manifest["resources"]
                       if resource["id"] == "fixture.feature")
        feature["version"] = version
        feature["sha256"] = hashlib.sha256((source / "feature.js").read_bytes()).hexdigest()
        feature["source_revision"] = "sha256:" + feature["sha256"]
        manifest["entrypoints"]["page"]["uses"][1]["version"] = version
        (source / "pack.json").write_text(json.dumps(manifest, indent=2) + "\n")
        return source

    def artifact(self, source, name):
        output = self.root / name
        build_artifact(source, output)
        return output

    def sibling_source(self, pack_id="fixture.installed-page-second", *, shared_version="1.0.0",
                       shared_suffix="", origins=None):
        source = self.root / pack_id
        shutil.copytree(SOURCE, source)
        (source / "second.js").write_text("window.secondPack = true;\n")
        if shared_suffix:
            with (source / "ui.js").open("a") as handle:
                handle.write("\n// " + shared_suffix + "\n")
        manifest = json.loads((source / "pack.json").read_text())
        manifest["id"] = pack_id
        manifest["files"].append("second.js")
        shared_digest = hashlib.sha256((source / "ui.js").read_bytes()).hexdigest()
        feature_digest = hashlib.sha256((source / "second.js").read_bytes()).hexdigest()
        manifest["resources"] = [
            {"contract": "tap.page-resource/v1", "id": "fixture.ui",
             "version": shared_version, "kind": "browser-classic-script",
             "file": "ui.js", "sha256": shared_digest, "license": "MIT",
             "source_revision": "sha256:" + shared_digest},
            {"contract": "tap.page-resource/v1", "id": pack_id + ".feature",
             "version": "0.1.0", "kind": "browser-classic-script",
             "file": "second.js", "sha256": feature_digest, "license": "MIT",
             "source_revision": "sha256:" + feature_digest},
        ]
        manifest["entrypoints"]["page"]["uses"] = [
            {"id": "fixture.ui", "version": shared_version},
            {"id": pack_id + ".feature", "version": "0.1.0"},
        ]
        if origins is not None:
            manifest["access"]["origins"] = origins
        (source / "pack.json").write_text(json.dumps(manifest, indent=2) + "\n")
        return source

    def test_artifact_is_reproducible_and_installed_binding_uses_snapshot(self):
        first = self.artifact(SOURCE, "first.tap-pack")
        second = self.artifact(SOURCE, "second.tap-pack")
        self.assertEqual(hashlib.sha256(first.read_bytes()).digest(),
                         hashlib.sha256(second.read_bytes()).digest())

        installed = self.store.install(first)
        self.assertFalse(installed["enabled"])
        with self.assertRaisesRegex(PackError, "not granted"):
            self.store.enable(PACK_ID, "0.1.0")

        result = self.store.enable(PACK_ID, "0.1.0",
                                   origins=ORIGINS, capabilities=CAPABILITIES)
        self.assertTrue(result["enabled"])
        effective = self.store.effective_bridge(bridge())
        self.assertEqual(effective_configuration(self.profile, bridge()), effective)
        self.assertEqual(effective["allow_origins"], ORIGINS)
        self.assertEqual([Path(path).parent.parent.name for path in effective["page_scripts"]],
                         ["fixture.ui", "fixture.feature"])
        self.assertTrue(all(str(self.profile / "resources/page") in path
                            for path in effective["page_scripts"]))
        self.assertFalse((self.profile / "state/packs" / PACK_ID).exists())

        Path(effective["page_scripts"][1]).write_text("tampered")
        with self.assertRaisesRegex(PackError, "shared page resource"):
            self.store.effective_bridge(bridge())

    def test_update_rollback_disable_and_uninstall_preserve_owned_data(self):
        first = self.artifact(SOURCE, "v1.tap-pack")
        second = self.artifact(self.source_version("0.2.0", "version two"), "v2.tap-pack")
        self.store.install(first)
        self.store.enable(PACK_ID, "0.1.0",
                          origins=ORIGINS, capabilities=CAPABILITIES)
        self.assertEqual(self.store.update(second)["version"], "0.2.0")
        self.assertEqual(self.store.status()["packs"][PACK_ID]["selected"],
                         "0.2.0")
        self.assertEqual(self.store.rollback(PACK_ID)["version"], "0.1.0")

        for parent in ("state", "data", "logs"):
            owned = self.profile / parent / "packs" / PACK_ID
            owned.mkdir(parents=True)
            (owned / "keep").write_text("retained")
        self.store.disable(PACK_ID)
        removed = self.store.uninstall(PACK_ID)
        self.assertTrue(removed["state_retained"])
        self.assertTrue(removed["shared_resources_retained"])
        self.assertFalse((self.profile / "packs" / PACK_ID / "versions/0.1.0").exists())
        self.assertFalse((self.profile / "packs" / PACK_ID / "versions/0.2.0").exists())
        for parent in ("state", "data", "logs"):
            self.assertEqual((self.profile / parent / "packs" / PACK_ID / "keep").read_text(),
                             "retained")

    def test_failed_update_installs_but_does_not_activate_new_version(self):
        first = self.artifact(SOURCE, "v1.tap-pack")
        source = self.source_version("0.2.0", "requests another origin")
        manifest = json.loads((source / "pack.json").read_text())
        manifest["access"]["origins"].append("https://second.fixture.example")
        (source / "pack.json").write_text(json.dumps(manifest, indent=2) + "\n")
        second = self.artifact(source, "v2.tap-pack")
        self.store.install(first)
        self.store.enable(PACK_ID, "0.1.0",
                          origins=ORIGINS, capabilities=CAPABILITIES)
        with self.assertRaisesRegex(PackError, "not granted"):
            self.store.update(second)
        record = self.store.status()["packs"][PACK_ID]
        self.assertEqual(record["selected"], "0.1.0")
        self.assertTrue(record["enabled"])
        self.assertIn("0.2.0", record["versions"])

    def test_same_origin_packs_merge_declarations_and_inject_shared_resource_once(self):
        first = self.artifact(SOURCE, "first.tap-pack")
        second = self.artifact(self.sibling_source(), "second.tap-pack")
        self.store.install(first)
        self.store.install(second)
        self.store.enable(PACK_ID, "0.1.0",
                          origins=ORIGINS, capabilities=CAPABILITIES)
        self.store.enable("fixture.installed-page-second", "0.1.0",
                          origins=ORIGINS, capabilities=CAPABILITIES)

        effective = self.store.effective_bridge(bridge())
        resource_ids = [Path(path).parent.parent.name for path in effective["page_scripts"]]
        self.assertEqual(resource_ids, ["fixture.ui", "fixture.feature",
                                        "fixture.installed-page-second.feature"])
        self.assertEqual(resource_ids.count("fixture.ui"), 1)
        self.assertEqual(effective["page_script_origins"], [ORIGINS, ORIGINS, ORIGINS])
        self.assertEqual(effective_configuration(self.profile, bridge()), effective)

    def test_shared_resource_version_or_content_conflict_fails_before_activation(self):
        first = self.artifact(SOURCE, "first.tap-pack")
        wrong_version = self.artifact(
            self.sibling_source("fixture.version-conflict", shared_version="2.0.0"),
            "wrong-version.tap-pack")
        wrong_content = self.artifact(
            self.sibling_source("fixture.content-conflict", shared_suffix="different bytes"),
            "wrong-content.tap-pack")
        for artifact in (first, wrong_version, wrong_content):
            self.store.install(artifact)
        self.store.enable(PACK_ID, "0.1.0",
                          origins=ORIGINS, capabilities=CAPABILITIES)
        with self.assertRaisesRegex(PackError, "conflicting versions"):
            self.store.enable("fixture.version-conflict", "0.1.0",
                              origins=ORIGINS, capabilities=CAPABILITIES)
        with self.assertRaisesRegex(PackError, "conflicting content"):
            self.store.enable("fixture.content-conflict", "0.1.0",
                              origins=ORIGINS, capabilities=CAPABILITIES)
        registry = self.store.status()["packs"]
        self.assertFalse(registry["fixture.version-conflict"]["enabled"])
        self.assertFalse(registry["fixture.content-conflict"]["enabled"])

    def test_archive_traversal_is_rejected_before_install(self):
        artifact = self.root / "bad.tap-pack"
        with tarfile.open(artifact, "w:gz") as archive:
            info = tarfile.TarInfo("../outside")
            info.size = 1
            archive.addfile(info, io.BytesIO(b"x"))
        with self.assertRaisesRegex(PackError, "unsafe"):
            self.store.install(artifact)
        self.assertFalse((self.root / "outside").exists())

    def test_symlinked_pack_parent_is_rejected_before_install_or_verify(self):
        artifact = self.artifact(SOURCE, "pack.tap-pack")
        outside = self.root / "outside-pack"
        outside.mkdir()
        self.store.code.mkdir()
        pack_parent = self.store.code / PACK_ID
        pack_parent.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(PackError, "must not traverse a symlink"):
            self.store.install(artifact)
        self.assertFalse((outside / "versions").exists())

        pack_parent.unlink()
        self.store.install(artifact)
        real_parent = self.root / "installed-pack-parent"
        pack_parent.rename(real_parent)
        pack_parent.symlink_to(real_parent, target_is_directory=True)
        registry = self.store.load()
        with self.assertRaisesRegex(PackError, "must not traverse a symlink"):
            self.store.verify(registry, PACK_ID, "0.1.0")

    def test_symlinked_shared_resource_parent_is_rejected(self):
        artifact = self.artifact(SOURCE, "pack.tap-pack")
        outside = self.root / "outside-resources"
        outside.mkdir()
        self.store.resources.mkdir()
        (self.store.resources / "page").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(PackError, "must not traverse a symlink"):
            self.store.install(artifact)
        self.assertEqual(list(outside.iterdir()), [])

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
            self.assertEqual(cli.main([*base, "enable", PACK_ID,
                                       "--version", "0.1.0",
                                       "--grant-origin", "https://fixture.example",
                                       "--grant-capability", "page.inject"]), 0)
            self.assertEqual(cli.main(["--profile", str(self.profile), "bridge", "explain",
                                       "--origin", "https://fixture.example"]), 0)
        self.assertIn('"allowed": true', output.getvalue())
        installed = self.store.effective_bridge(bridge())["page_scripts"]
        self.assertTrue(all(str(self.profile / "resources/page") in path for path in installed))


if __name__ == "__main__":
    unittest.main()
