import io
import json
from pathlib import Path
import tempfile
import unittest

from tap_core.pack_add import add, resolve
from tap_core.pack_store import build_artifact, PackStore
from tap_core.packs import PackError


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class PackAddTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pack-add-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        source = self.root / "source"
        self.source = source
        source.mkdir()
        (source / "command.py").write_text("print('fixture')\n")
        manifest = {
            "manifest_version": 1, "id": "fixture.add", "version": "1.2.3",
            "requires": {"pack_api": 1, "dependencies": []},
            "files": ["command.py"],
            "entrypoints": {"command": {"interface": "process-argv-v1",
                "runtime": "host-python", "file": "command.py", "commands": [{
                    "path": ["hello"], "summary": "Say hello", "usage": "[NAME]",
                    "profile": "required"}]}},
            "config": {"prefix": {"type": "string", "default": "fixture"}},
            "access": {"origins": ["https://example.test"],
                       "capabilities": ["command.execute"]}}
        (source / "pack.json").write_text(json.dumps(manifest))
        self.artifact = self.root / "fixture.add-1.2.3.tap-pack"
        build_artifact(source, self.artifact)
        self.release = [{"tag_name": "v1.2.3", "draft": False, "prerelease": False,
                         "assets": [{"name": self.artifact.name,
                                     "browser_download_url": "https://github.com/owner/repo/releases/download/v1.2.3/pack"}]}]

    def opener(self, request, timeout=0):
        if "/releases?" in request.full_url:
            return Response(json.dumps(self.release).encode())
        return Response(self.artifact.read_bytes())

    def test_add_confirms_records_source_and_is_idempotent(self):
        messages = []
        result = add(self.root / "profile", "owner/repo", opener=self.opener,
                     input_fn=lambda prompt: "yes", output_fn=messages.append)
        self.assertTrue(result["enabled"])
        self.assertIn("local executable code", " ".join(messages))
        metadata = PackStore(self.root / "profile").load()["packs"]["fixture.add"]["versions"]["1.2.3"]
        self.assertEqual(metadata["source"], "github:owner/repo@1.2.3")
        self.assertEqual(len(metadata["artifact_sha256"]), 64)
        repeated = add(self.root / "profile", "owner/repo", opener=self.opener,
                       input_fn=lambda _prompt: self.fail("must not ask again"), output_fn=messages.append)
        self.assertTrue(repeated["already_configured"])

    def test_reenable_preserves_config_and_noop_verifies_installed_files(self):
        profile = self.root / "profile"
        add(profile, "owner/repo", opener=self.opener, assume_yes=True,
            output_fn=lambda value: None)
        store = PackStore(profile)
        store.enable("fixture.add", "1.2.3", origins=["https://example.test"],
                     capabilities=["command.execute"], config={"prefix": "custom"})
        store.disable("fixture.add")
        add(profile, "owner/repo", opener=self.opener, assume_yes=True,
            output_fn=lambda value: None)
        self.assertEqual(store.load()["packs"]["fixture.add"]["config"],
                         {"prefix": "custom"})
        manifest_path = self.source / "pack.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["version"] = "1.2.4"
        manifest_path.write_text(json.dumps(manifest))
        newer = self.root / "fixture.add-1.2.4.tap-pack"
        build_artifact(self.source, newer)
        self.artifact = newer
        self.release = [{"tag_name": "v1.2.4", "draft": False, "prerelease": False,
                         "assets": [{"name": newer.name,
                                     "browser_download_url": "https://github.com/owner/repo/releases/download/v1.2.4/pack"}]}]
        add(profile, "owner/repo@1.2.4", opener=self.opener, assume_yes=True,
            output_fn=lambda value: None)
        updated = store.load()["packs"]["fixture.add"]
        self.assertEqual(updated["selected"], "1.2.4")
        self.assertEqual(updated["config"], {"prefix": "custom"})
        installed = store.version_root("fixture.add", "1.2.4") / "command.py"
        installed.write_text("changed\n")
        with self.assertRaisesRegex(PackError, "integrity check failed"):
            add(profile, "owner/repo", opener=self.opener, assume_yes=True,
                output_fn=lambda value: None)

    def test_cancel_and_prerelease_policy(self):
        with self.assertRaisesRegex(PackError, "cancelled"):
            add(self.root / "profile", "owner/repo", opener=self.opener,
                input_fn=lambda prompt: "no", output_fn=lambda value: None)
        self.assertEqual(PackStore(self.root / "profile").load()["packs"], {})
        self.release[0]["prerelease"] = True
        with self.assertRaisesRegex(PackError, "no stable release"):
            resolve("owner/repo", self.opener)
        self.assertEqual(resolve("owner/repo@1.2.3", self.opener)["version"], "1.2.3")


if __name__ == "__main__":
    unittest.main()
