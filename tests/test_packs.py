import copy
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tap_core.packs import (PackError, check_activation, fixture_context, load_manifest,
                            resolve_config, validate_manifest)
from tap_core.page_resources import CONTRACT, digest
from tools.check_pack_fixtures import FIXTURES, prepare, run_page, run_python


class PackManifestTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="tap-pack-test-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / "pack"
        shutil.copytree(FIXTURES / "reader", self.root)
        self.manifest = load_manifest(self.root)

    def invalid(self, mutate, message):
        manifest = copy.deepcopy(self.manifest)
        mutate(manifest)
        with self.assertRaisesRegex(PackError, message):
            validate_manifest(manifest, self.root)

    def resource(self, root, resource_id, file):
        return {"contract": CONTRACT, "id": resource_id, "version": "1.0.0",
                "kind": "browser-classic-script", "file": file,
                "sha256": digest(root / file), "license": "MIT",
                "source_revision": "fixture-v1"}

    def test_both_fixture_manifests(self):
        for name in ("reader", "page-bridge"):
            self.assertEqual(load_manifest(FIXTURES / name)["requires"]["pack_api"], 1)

    @unittest.skipUnless(shutil.which("bun"), "Bun required only for the page runtime control fixture")
    def test_page_runtime_document_controls(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [shutil.which("bun"), str(root / "tests/page_runtime_controls.cjs"),
             str(root / "tap_core/page-runtime.js")],
            text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("PASS connection controls", result.stdout)

    def test_versions_and_unknown_fields(self):
        for version in (0, 2, True, "1"):
            self.invalid(lambda m: m.update(manifest_version=version), "manifest_version")
        self.invalid(lambda m: m["requires"].update(pack_api=2), "incompatible")
        self.invalid(lambda m: m["requires"].update(pack_api=True), "incompatible")
        self.invalid(lambda m: m.update(version="latest"), "version")
        self.invalid(lambda m: m.update(typo=True), "unknown fields")
        self.invalid(lambda m: m.pop("config"), "missing fields")

    def test_static_features_are_bounded_plain_text(self):
        self.manifest["features"] = [
            {"id": "session.archive", "label": "Session archive", "value": "Versioned JSON",
             "folder": "data/readers/chatgpt.sessions"},
        ]
        validate_manifest(self.manifest, self.root)
        self.invalid(lambda m: m.update(features=[{"id": "bad", "label": "", "value": "x"}]),
                     "label")
        self.invalid(lambda m: m.update(features=[
            {"id": "same", "label": "One", "value": "x"},
            {"id": "same", "label": "Two", "value": "y"},
        ]), "duplicate")
        self.invalid(lambda m: m.update(features=[
            {"id": "feature", "label": "Feature", "value": "x", "command": "run"},
        ]), "unknown fields")
        for folder in ("/tmp/archive", "../archive", "readers/archive", "data/../archive", "data\\archive"):
            self.invalid(lambda m, folder=folder: m.update(features=[
                {"id": "archive", "label": "Archive", "value": "Local", "folder": folder},
            ]), "folder")

    def test_files_exist_are_declared_and_stay_inside_pack(self):
        for name in ("missing.py", "../outside.py", "/tmp/outside.py", "./reader.py", "x\\reader.py"):
            self.invalid(lambda m: m.update(files=[name]), "files")
        outside = Path(self.directory.name) / "outside.py"
        outside.write_text("raise RuntimeError('must not run')\n")
        (self.root / "escape.py").symlink_to(outside)
        self.invalid(lambda m: m.update(files=["escape.py"]), "outside pack")
        self.invalid(lambda m: m["entrypoints"]["reader"].update(file="undeclared.py"), "declared")
        self.invalid(lambda m: m.update(files=["reader.py", "reader.py"]), "duplicate")

    def test_exact_origin_rejections(self):
        for origin in ("https://*.example", "https://fixture.example/", "https://fixture.example?q=1",
                       "https://fixture.example#x", "https://user@fixture.example",
                       "HTTPS://fixture.example", "https://fixture.example:443", "file://fixture.example",
                       "https://fixture.example:0", "https://fixture.example:", "https://fixture.example:99999"):
            self.invalid(lambda m: m["access"].update(origins=[origin]), "origins")
        self.invalid(lambda m: m["access"].update(origins=[]), "exact origin")
        manifest = copy.deepcopy(self.manifest)
        manifest["access"]["origins"] = ["http://127.0.0.1:18080"]
        validate_manifest(manifest, self.root)

    def test_entrypoint_and_capability_consistency(self):
        self.invalid(lambda m: m["entrypoints"]["reader"].update(interface="shell"), "interface")
        self.invalid(lambda m: m["entrypoints"].update(workflow={}), "unsupported role")
        self.invalid(lambda m: m["access"].update(capabilities=[]), "missing capability")
        self.invalid(lambda m: m["access"].update(capabilities=["capture.read", "shell.anything"]), "unknown capability")

    def test_page_and_handler_are_independent_roles(self):
        for role, capability in (("page", "page.inject"), ("handler", "bridge.handle")):
            manifest = load_manifest(FIXTURES / "page-bridge")
            manifest["entrypoints"] = {role: manifest["entrypoints"][role]}
            manifest["access"]["capabilities"] = [capability]
            validate_manifest(manifest, FIXTURES / "page-bridge")

    def test_ordered_classic_page_scripts_are_a_distinct_interface(self):
        root = FIXTURES / "page-bridge"
        manifest = load_manifest(root)
        manifest["resources"] = [self.resource(root, "fixture.ui", "page.js"),
                                 self.resource(root, "fixture.feature", "handler.py")]
        manifest["entrypoints"] = {"page": {"interface": "browser-scripts-v1",
                                                   "uses": [
                                                       {"id": "fixture.ui", "version": "1.0.0"},
                                                       {"id": "fixture.feature", "version": "1.0.0"}]}}
        manifest["access"]["capabilities"] = ["page.inject"]
        validate_manifest(manifest, root)
        manifest["entrypoints"]["page"]["uses"][1]["version"] = "2.0.0"
        with self.assertRaisesRegex(PackError, "missing provider"):
            validate_manifest(manifest, root)

    def test_classic_page_resource_identity_and_version_are_explicit(self):
        root = FIXTURES / "page-bridge"
        manifest = load_manifest(root)
        manifest["resources"] = [self.resource(root, "shared.ui", "page.js"),
                                 self.resource(root, "feature.two", "handler.py")]
        manifest["entrypoints"] = {"page": {"interface": "browser-scripts-v1",
                                                   "uses": [
                                                       {"id": "shared.ui", "version": "1.0.0"},
                                                       {"id": "shared.ui", "version": "1.0.0"}]}}
        manifest["access"]["capabilities"] = ["page.inject"]
        with self.assertRaisesRegex(PackError, "duplicate resource id"):
            validate_manifest(manifest, root)
        manifest["entrypoints"]["page"]["uses"][1]["id"] = "feature.two"
        manifest["entrypoints"]["page"]["uses"][1]["version"] = "latest"
        with self.assertRaisesRegex(PackError, "MAJOR.MINOR.PATCH"):
            validate_manifest(manifest, root)

    def test_typed_configuration(self):
        self.assertEqual(resolve_config(self.manifest), {"prefix": "seen"})
        self.assertEqual(resolve_config(self.manifest, {"prefix": "changed"}), {"prefix": "changed"})
        self.assertEqual(self.manifest["config"]["prefix"]["default"], "seen")
        for overrides in ({"unknown": 1}, {"prefix": 1}, []):
            with self.assertRaises(PackError):
                resolve_config(self.manifest, overrides)
        self.invalid(lambda m: m["config"].update(count={"type": "integer", "default": True}), "wrong type")
        self.invalid(lambda m: m["config"].update(count={"type": "number", "default": 1}), "unsupported type")

    def test_request_is_not_an_access_grant(self):
        with self.assertRaisesRegex(PackError, "not granted"):
            check_activation(self.manifest, [], ["capture.read"], {})
        with self.assertRaisesRegex(PackError, "not granted"):
            check_activation(self.manifest, ["https://fixture.example"], [], {})
        with self.assertRaisesRegex(PackError, "not granted"):
            check_activation(self.manifest, ["https://fixture.example:8443"], ["capture.read"], {})
        check_activation(self.manifest, ["https://fixture.example"], ["capture.read"], {})

    def test_dependencies_pinned_and_checked_against_host_inventory(self):
        self.invalid(lambda m: m["requires"].update(dependencies=[{"id": "helper", "version": "latest"}]), "exact")
        self.manifest["requires"]["dependencies"] = [{"id": "helper", "version": "1.2.3"}]
        validate_manifest(self.manifest, self.root)
        for inventory in ({}, {"helper": "1.2.4"}):
            with self.assertRaisesRegex(PackError, "expected installed version"):
                check_activation(self.manifest, ["https://fixture.example"], ["capture.read"], inventory)
        check_activation(self.manifest, ["https://fixture.example"], ["capture.read"], {"helper": "1.2.3"})

    def test_pack_cannot_depend_on_itself_even_if_installed(self):
        self.manifest["requires"]["dependencies"] = [
            {"id": self.manifest["id"], "version": self.manifest["version"]}]
        with self.assertRaisesRegex(PackError, "cannot depend on itself"):
            validate_manifest(self.manifest, self.root)
        inventory = {self.manifest["id"]: self.manifest["version"]}
        with self.assertRaisesRegex(PackError, "cannot depend on itself"):
            check_activation(self.manifest, ["https://fixture.example"], ["capture.read"], inventory)

    def test_validation_does_not_execute_code_and_prepare_rejects_before_start(self):
        marker = Path(self.directory.name) / "executed"
        (self.root / "reader.py").write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
        load_manifest(self.root)
        self.assertFalse(marker.exists())
        self.manifest["requires"]["pack_api"] = 999
        (self.root / "pack.json").write_text(json.dumps(self.manifest))
        profile = Path(self.directory.name) / "profile"
        with patch("subprocess.run", side_effect=AssertionError("must not execute")):
            with self.assertRaisesRegex(PackError, "incompatible"):
                prepare(self.root, profile)
        self.assertFalse(marker.exists())
        self.assertFalse(profile.exists())

    def test_duplicate_json_keys_are_rejected(self):
        (self.root / "pack.json").write_text('{"id":"one","id":"two"}')
        with self.assertRaisesRegex(PackError, "duplicate key"):
            load_manifest(self.root)

    def test_state_paths_are_separate_per_profile_and_pack(self):
        first = fixture_context(self.manifest, Path(self.directory.name) / "profile-a")
        second = fixture_context(self.manifest, Path(self.directory.name) / "profile-b")
        page = fixture_context(load_manifest(FIXTURES / "page-bridge"), Path(self.directory.name) / "profile-a")
        for key in ("state_dir", "output_dir", "log_dir"):
            self.assertNotEqual(first[key], second[key])
            self.assertNotEqual(first[key], page[key])
            self.assertNotIn(self.root, Path(first[key]).parents)
            self.assertFalse(Path(first[key]).exists())

    def test_cli_clear_failure_and_validation_only_success(self):
        result = subprocess.run([sys.executable, "-m", "tap_core.packs", str(self.root)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIs(json.loads(result.stdout)["activated"], False)
        result = subprocess.run([sys.executable, "-m", "tap_core.packs", str(self.root), "--host-api", "2"],
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("incompatible", result.stderr)

    def without_pack_writes(self, check):
        before = {path.relative_to(FIXTURES): path.read_bytes() for path in FIXTURES.rglob("*") if path.is_file()}
        result = check()
        after = {path.relative_to(FIXTURES): path.read_bytes() for path in FIXTURES.rglob("*") if path.is_file()}
        self.assertEqual(before, after, "pack execution must not write into installed code")
        return result

    def test_python_reader_handler_and_mutator_fixtures(self):
        result = self.without_pack_writes(run_python)
        for role in ("reader", "handler", "mutator"):
            self.assertEqual(result[role], "passed")
        self.assertEqual(result["live_ws"], "not tested")

    @unittest.skipUnless(shutil.which("bun"), "Bun required only for executing the browser-module fixture")
    def test_page_to_handler_fixture(self):
        result = self.without_pack_writes(lambda: run_page(Path(shutil.which("bun"))))
        self.assertEqual(result["page_to_handler_shape"], "passed")


if __name__ == "__main__":
    unittest.main()
