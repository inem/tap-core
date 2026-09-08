import copy
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from tap_core import cli
from tap_core.commands import discover, execute
from tap_core.pack_store import PackStore, build_artifact
from tap_core.packs import PackError, load_manifest, validate_manifest
from tap_core.runtime import Profile


ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "fixtures/packs/command"
ORIGIN = "https://fixture.example"
CAPABILITY = "command.execute"


class CommandHostTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="tap-command-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.profile = self.root / "profile"
        self.artifact = self.root / "fixture.tap-pack"
        build_artifact(SOURCE, self.artifact)

    def cli(self, *arguments):
        output, error = io.StringIO(), io.StringIO()
        with patch("tap_core.cli.platform.system", return_value="Darwin"), \
                redirect_stdout(output), redirect_stderr(error):
            code = cli.main(["--profile", str(self.profile), *arguments])
        return code, output.getvalue(), error.getvalue()

    def install_enable(self):
        self.assertEqual(self.cli("pack", "install", str(self.artifact))[0], 0)
        code, _output, error = self.cli(
            "pack", "enable", "fixture.command", "--version", "0.1.0",
            "--grant-origin", ORIGIN, "--grant-capability", CAPABILITY)
        self.assertEqual(code, 0, error)

    def run_tap(self, *arguments, environment=None):
        return subprocess.run(
            [sys.executable, "-B", str(ROOT / "tap"), "--profile", str(self.profile),
             *arguments], text=True, capture_output=True, env=environment,
        )

    def test_manifest_declares_commands_without_importing_provider(self):
        marker = self.root / "executed"
        with patch.dict(os.environ, {"TAP_FIXTURE_EXECUTION_MARKER": str(marker)}):
            manifest = load_manifest(SOURCE)
        self.assertEqual(manifest["entrypoints"]["command"]["interface"], "process-argv-v1")
        self.assertFalse(marker.exists())
        invalid = copy.deepcopy(manifest)
        invalid["entrypoints"]["command"]["commands"][0]["path"] = ["Bad_Name"]
        with self.assertRaisesRegex(PackError, "lowercase command words"):
            validate_manifest(invalid, SOURCE)
        invalid = copy.deepcopy(manifest)
        invalid["entrypoints"]["command"]["runtime"] = "implicit-path-bun"
        with self.assertRaisesRegex(PackError, "host-python"):
            validate_manifest(invalid, SOURCE)

    def test_command_only_pack_uses_bare_profile_and_help_does_not_execute(self):
        self.install_enable()
        self.assertFalse((self.profile / "profile.json").exists())
        marker = self.root / "executed"
        environment = {**os.environ, "TAP_FIXTURE_EXECUTION_MARKER": str(marker)}
        result = self.run_tap("fixture", "echo", "--help", environment=environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("fixture.command@0.1.0", result.stdout)
        self.assertIn("help does not run provider code", result.stdout)
        self.assertFalse(marker.exists())
        result = self.run_tap("--help", environment=environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("fixture echo", result.stdout)
        self.assertFalse(marker.exists())

    def test_existing_where_runs_through_registry_with_same_result_shape(self):
        profile = Profile(self.profile, "/fixture/mitmdump", 19001, "explicit",
                          "http://example.test", [])
        profile.save()
        code, output, error = self.cli("where")
        self.assertEqual(code, 0, error)
        result = json.loads(output)
        self.assertEqual(result["profile"], str(self.profile.resolve()))
        self.assertEqual(result["backend"], "/fixture/mitmdump")
        help_result = self.run_tap("where", "--help")
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn("tap-core@development (builtin)", help_result.stdout)

    def test_argv_streams_and_exit_code_are_preserved_without_shell(self):
        self.install_enable()
        arguments = ["two words", "$(touch never)", "semi;colon", "quotes'\""]
        result = self.run_tap("fixture", "echo", *arguments)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["argv"], arguments)
        self.assertEqual(payload["provider"], {"id": "fixture.command", "version": "0.1.0"})
        self.assertFalse((self.profile / "state/packs/fixture.command/never").exists())
        failed = self.run_tap("fixture", "fail", "7")
        self.assertEqual(failed.returncode, 7)
        self.assertIn("fixture provider failure", failed.stderr)

    def test_collisions_fail_before_activation_and_leave_selection_unchanged(self):
        self.install_enable()
        source = self.root / "collision"
        shutil.copytree(SOURCE, source)
        manifest = json.loads((source / "pack.json").read_text())
        manifest["id"] = "fixture.collision"
        manifest["entrypoints"]["command"]["commands"] = [{
            "path": ["fixture"], "summary": "Ambiguous prefix", "usage": "ARG",
            "profile": "required",
        }]
        (source / "pack.json").write_text(json.dumps(manifest, indent=2) + "\n")
        artifact = self.root / "collision.tap-pack"
        build_artifact(source, artifact)
        PackStore(self.profile).install(artifact)
        with self.assertRaisesRegex(PackError, "conflicts"):
            PackStore(self.profile).enable(
                "fixture.collision", "0.1.0", origins=[ORIGIN], capabilities=[CAPABILITY])
        self.assertFalse(PackStore(self.profile).status()["packs"]["fixture.collision"]["enabled"])

        manifest["entrypoints"]["command"]["commands"][0]["path"] = ["off", "now"]
        validate_manifest(manifest, source)
        with self.assertRaisesRegex(PackError, "built-in root"):
            from tap_core.commands import validate_external_command_paths
            validate_external_command_paths([("fixture.collision", manifest)])

    def test_missing_declared_dependency_refuses_activation(self):
        source = self.root / "dependency"
        shutil.copytree(SOURCE, source)
        manifest = json.loads((source / "pack.json").read_text())
        manifest["id"] = "fixture.dependency"
        manifest["requires"]["dependencies"] = [{"id": "helper", "version": "1.2.3"}]
        manifest["entrypoints"]["command"]["commands"][0]["path"] = ["dependency", "echo"]
        manifest["entrypoints"]["command"]["commands"] = \
            manifest["entrypoints"]["command"]["commands"][:1]
        (source / "pack.json").write_text(json.dumps(manifest, indent=2) + "\n")
        artifact = self.root / "dependency.tap-pack"
        build_artifact(source, artifact)
        store = PackStore(self.profile)
        store.install(artifact)
        with self.assertRaisesRegex(PackError, "expected installed version"):
            store.enable("fixture.dependency", "0.1.0", origins=[ORIGIN],
                         capabilities=[CAPABILITY])

    def test_signal_exit_is_normalized_and_streams_are_inherited(self):
        self.install_enable()
        command = discover(self.profile).commands[("fixture", "echo")]
        process = Mock()
        process.wait.return_value = -2
        with patch("tap_core.commands.subprocess.Popen", return_value=process) as popen:
            self.assertEqual(execute(command, self.profile, ["value"]), 130)
        call = popen.call_args
        self.assertIsNone(call.kwargs["stdin"])
        self.assertIsNone(call.kwargs["stdout"])
        self.assertIsNone(call.kwargs["stderr"])

    def test_running_invocation_holds_version_lease_against_disable(self):
        self.install_enable()
        release = self.root / "release"
        process = subprocess.Popen(
            [sys.executable, "-B", str(ROOT / "tap"), "--profile", str(self.profile),
             "fixture", "wait", str(release)], text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.addCleanup(release.touch)
        self.addCleanup(lambda: process.poll() is None and process.kill())
        self.assertEqual(process.stdout.readline().strip(), "ready")
        blocked = self.run_tap("pack", "disable", "fixture.command")
        self.assertEqual(blocked.returncode, 1)
        self.assertIn("still running", blocked.stderr)
        release.touch()
        stdout, stderr = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0, stdout + stderr)
        disabled = self.run_tap("pack", "disable", "fixture.command")
        self.assertEqual(disabled.returncode, 0, disabled.stderr)
        self.assertFalse(PackStore(self.profile).status()["packs"]["fixture.command"]["enabled"])

    def test_broken_external_code_does_not_block_core_recovery_path(self):
        profile = Profile(self.profile, "/fixture/mitmdump", 19001, "explicit",
                          "http://example.test", [])
        profile.save()
        self.install_enable()
        command_file = self.profile / "packs/fixture.command/versions/0.1.0/command.py"
        command_file.write_text("broken after activation\n")
        registry = discover(self.profile)
        self.assertNotIn(("fixture", "echo"), registry.commands)
        self.assertTrue(any("integrity" in item for item in registry.diagnostics))
        disabled, _output, error = self.cli("pack", "disable", "fixture.command")
        self.assertEqual(disabled, 0, error)
        with patch("tap_core.cli.platform.system", return_value="Darwin"), \
                patch("tap_core.cli.mutate", return_value="recovered") as mutate:
            code = cli.main(["--profile", str(self.profile), "off"])
        self.assertEqual(code, 0)
        mutate.assert_called_once()


if __name__ == "__main__":
    unittest.main()
