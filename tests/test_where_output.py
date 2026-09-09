import copy
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tap_core.cli import main
from tap_core.projection import ProjectionError
from tap_core.runtime import Profile
from tap_core.where_view import (load_material, project_where, public_where_result,
                                 terminal_where, validate_where_result)


ROOT = Path(__file__).resolve().parent.parent
RAW = json.loads((ROOT / "fixtures/where/current.json").read_text())


class WhereProjectionTests(unittest.TestCase):
    def test_saved_raw_result_becomes_versioned_address_only_result(self):
        result = public_where_result(RAW)
        self.assertEqual(result["schema"], "tap.where-result/v1")
        self.assertEqual(len(result["locations"]), 15)
        profile = result["locations"][0]
        self.assertEqual(profile, {
            "id": "profile", "section": "profile", "role": "profile-root",
            "owner": "profile", "label": "profile", "order": 1,
            "address": {"knowledge": "known", "value": "/fixture/profile"},
        })
        component = next(item for item in result["locations"]
                         if item["id"] == "component_launch_agent")
        self.assertEqual(component["address"]["knowledge"], "unknown")
        self.assertEqual(component["address"]["reason"], "not_observed")
        self.assertNotIn("present", json.dumps(result))
        self.assertNotIn("healthy", json.dumps(result))

    def test_material_owns_location_group_role_owner_label_and_order(self):
        material = copy.deepcopy(load_material())
        config = next(item for item in material["locations"] if item["id"] == "config")
        config.update({"label": "settings", "section": "runtime", "order": 3,
                       "role": "alternate-config", "owner": "fixture"})
        config["source"] = {"path": ["alternate"], "error": "alternate"}
        raw = {**RAW, "alternate": "/fixture/alternate"}
        result = public_where_result(raw, material)
        changed = next(item for item in result["locations"] if item["id"] == "config")
        self.assertEqual((changed["label"], changed["section"], changed["role"],
                          changed["owner"], changed["address"]["value"]),
                         ("settings", "runtime", "alternate-config", "fixture",
                          "/fixture/alternate"))
        text = terminal_where(result, material=material)
        self.assertIn("settings", text)
        self.assertIn("/fixture/alternate", text)

    def test_projection_builds_nested_sections_and_keeps_provenance(self):
        projection = project_where(public_where_result(RAW))
        slots = {slot.identifier: slot for slot in projection.document.slots}
        self.assertEqual(slots["section-profile"].parent, "where")
        self.assertEqual(slots["entry-config"].parent, "section-profile")
        self.assertEqual(slots["entry-config-address"].parent, "entry-config")
        self.assertEqual(len(projection.meanings), 15)
        config = next(atom for atom in projection.meanings if atom.arguments[0] == "config")
        derivation = projection.provenance[config]
        self.assertEqual(derivation.operator, "select:max-rank")
        candidate = derivation.warrants[0]
        supplied = projection.provenance[candidate].warrants[0]
        self.assertEqual(projection.provenance[supplied].operator, "supplied-where-result")
        attributes = {atom.arguments[1]: atom.arguments[2]
                      for atom in projection.provenance
                      if atom.relation == "attribute" and atom.arguments[0] == "config"}
        self.assertEqual(attributes,
                         {"label": "config", "order": 2, "owner": "profile",
                          "role": "configuration", "section": "profile"})

    def test_source_declaration_order_does_not_change_public_order(self):
        material = copy.deepcopy(load_material())
        baseline = public_where_result(RAW, material)
        material["locations"].reverse()
        self.assertEqual(public_where_result(dict(reversed(list(RAW.items()))), material),
                         baseline)

    def test_configured_component_addresses_are_known_without_health_claims(self):
        raw = {**RAW,
               "component_launch_agent": "/fixture/component.plist",
               "component_log": "/fixture/profile/logs/components.log",
               "handler_logs": "/fixture/profile/logs/handlers"}
        result = public_where_result(raw)
        components = [item for item in result["locations"]
                      if item["id"] in {"component_launch_agent", "component_log",
                                        "handler_logs"}]
        self.assertTrue(all(item["address"]["knowledge"] == "known"
                            for item in components))

    def test_terminal_tree_preserves_known_and_unknown_addresses(self):
        text = terminal_where(public_where_result(RAW), color=False)
        self.assertEqual(text, "\n".join([
            "Profile",
            "  profile            /fixture/profile",
            "  config             /fixture/profile/profile.json",
            "  data               /fixture/profile/data",
            "  state              /fixture/profile/state",
            "  certificates       /fixture/profile/certificates",
            "  capture log        /fixture/profile/logs/capture.log",
            "  packs              /fixture/profile/packs",
            "  resources          /fixture/profile/resources",
            "  pack registry      /fixture/profile/state/pack-registry.json",
            "System bindings",
            "  capture service    /fixture/Library/LaunchAgents/tap.fixture.plist",
            "  component service  ? not observed",
            "Component bindings",
            "  component log      ? not observed",
            "  handler logs       ? not observed",
            "Runtime and source",
            "  backend            /fixture/runtime/mitmdump",
            "  core checkout      /fixture/checkout",
        ]))

    def test_narrow_width_refuses_instead_of_dropping_an_address(self):
        result = public_where_result(RAW)
        with self.assertRaisesRegex(ProjectionError, "required content exceeds width 20"):
            terminal_where(result, width=20)

    def test_result_validation_rejects_metadata_or_address_drift(self):
        result = public_where_result(RAW)
        changed = copy.deepcopy(result)
        changed["locations"][0]["owner"] = "filesystem-probe"
        with self.assertRaisesRegex(ProjectionError, "invalid where result location"):
            validate_where_result(changed)
        changed = copy.deepcopy(result)
        changed["locations"][0]["address"]["value"] = None
        with self.assertRaisesRegex(ProjectionError, "invalid known where address"):
            validate_where_result(changed)

    def test_shared_runner_contains_no_status_or_where_domain_vocabulary(self):
        source = (ROOT / "tap_core/material_view.py").read_text()
        for word in ("running", "stopped", "routing", "profile.json", "launch_agent"):
            self.assertNotIn(word, source)


class WhereCliTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="tap-where-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        Profile(self.root, "/fixture/mitmdump", 19001, "explicit",
                "http://example.test", []).save()

    def invoke(self, *arguments, tty=False):
        class Output(io.StringIO):
            def isatty(self):
                return tty
        out, err = Output(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(["--profile", str(self.root), "where", *arguments])
        return code, out.getvalue(), err.getvalue()

    def test_default_raw_output_remains_the_existing_shape(self):
        code, output, error = self.invoke()
        self.assertEqual((code, error), (0, ""))
        result = json.loads(output)
        self.assertEqual(result["profile"], str(self.root))
        self.assertEqual(result["config"], str(self.root / "profile.json"))
        self.assertEqual(result["backend"], "/fixture/mitmdump")
        self.assertNotIn("schema", result)
        self.assertEqual(output, json.dumps(result, indent=2) + "\n")

    def test_semantic_and_terminal_modes_use_the_same_result(self):
        code, output, error = self.invoke("--output", "semantic-json")
        self.assertEqual((code, error), (0, ""))
        semantic = json.loads(output)
        self.assertEqual(semantic["schema"], "tap.where-result/v1")
        code, terminal, error = self.invoke("--output", "terminal", "--color", "never")
        self.assertEqual((code, error), (0, ""))
        self.assertIn(str(self.root / "profile.json"), terminal)
        self.assertIn("? not observed", terminal)

    def test_raw_and_semantic_json_ignore_terminal_options(self):
        self.assertEqual(self.invoke()[1],
                         self.invoke("--width", "1", "--color", "always")[1])
        baseline = self.invoke("--output", "semantic-json")[1]
        decorated = self.invoke("--output", "semantic-json", "--width", "1",
                                "--color", "always", tty=True)[1]
        self.assertEqual(decorated, baseline)
        self.assertNotIn("\x1b[", decorated)

    def test_terminal_width_failure_is_explicit_and_raw_recovers(self):
        code, output, error = self.invoke("--output", "terminal", "--width", "1")
        self.assertEqual((code, output), (1, ""))
        self.assertIn("required content exceeds width 1", error)
        code, output, error = self.invoke()
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(json.loads(output)["profile"], str(self.root))

    def test_color_auto_only_decorates_terminal_tty(self):
        plain = self.invoke("--output", "terminal", tty=False)[1]
        colored = self.invoke("--output", "terminal", tty=True)[1]
        self.assertNotIn("\x1b[", plain)
        self.assertIn("\x1b[1m", colored)

    def test_bad_option_returns_one_without_traceback(self):
        code, output, error = self.invoke("--filesystem-health")
        self.assertEqual((code, output), (1, ""))
        self.assertIn("unrecognized arguments", error)

    def test_dispatcher_help_advertises_output_contract_without_execution(self):
        code, output, error = self.invoke("--help")
        self.assertEqual((code, error), (0, ""))
        self.assertIn("--output raw-json|semantic-json|terminal", output)


if __name__ == "__main__":
    unittest.main()
