import copy
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tap_core.cli import main
from tap_core.filesystem_sensor import inspect_path
from tap_core.projection import ProjectionError
from tap_core.runtime import Profile
from tap_core.where_view import (PRESENCE_MATERIAL, capture_filesystem_receipts,
                                 load_carrier_adapter, load_filesystem_adapter,
                                 load_material, observed_where_result, project_where,
                                 public_filesystem_result, public_where_result,
                                 terminal_where, validate_where_result,
                                 with_filesystem_observations)


ROOT = Path(__file__).resolve().parent.parent
RAW = json.loads((ROOT / "fixtures/where/current.json").read_text())
NESTED_ADAPTER = ROOT / "tests/fixtures/where-carriers/nested.json"
CONTRACT_FIXTURE = ROOT / "contracts/where-result/v1/fixtures/basic.json"
V2_CONTRACT_FIXTURE = ROOT / "contracts/where-result/v2/fixtures/config-regular.json"
FILESYSTEM_CORPUS = ROOT / "tests/fixtures/where-filesystem/receipts.json"


class WhereProjectionTests(unittest.TestCase):
    def test_checked_in_contract_fixture_reproduces_both_projections(self):
        fixture = json.loads(CONTRACT_FIXTURE.read_text())
        semantic = public_where_result(fixture["raw_snapshot"])
        self.assertEqual(semantic, fixture["semantic_result"])
        self.assertEqual(terminal_where(semantic), fixture["terminal"]["unbounded"])

    def test_v2_contract_fixture_reproduces_receipt_to_terminal_chain(self):
        fixture = json.loads(V2_CONTRACT_FIXTURE.read_text())
        addresses = public_where_result(fixture["raw_snapshot"])
        filesystem = public_filesystem_result(fixture["sensor_receipts"])
        semantic = with_filesystem_observations(addresses, filesystem)
        self.assertEqual(semantic, fixture["semantic_result"])
        self.assertEqual(terminal_where(semantic), fixture["terminal"]["unbounded"])

    def test_saved_receipt_corpus_preserves_presence_kind_and_failure(self):
        corpus = json.loads(FILESYSTEM_CORPUS.read_text())
        for case in corpus["cases"]:
            with self.subTest(case=case["name"]):
                carrier = {
                    "schema": "tap.filesystem-sensor-receipts/v1",
                    "receipts": {"config": case["receipt"]},
                }
                result = public_filesystem_result(carrier)
                self.assertEqual(result["observations"]["config"], case["expected"])

    def test_present_filesystem_meanings_are_separate_and_both_rendered(self):
        result = observed_where_result(
            RAW, sensor=lambda path: {"outcome": "present", "kind": "symlink",
                                      "errno": None, "message": ""})
        projection = project_where(result)
        config = {(atom.arguments[1], atom.arguments[2]) for atom in projection.meanings
                  if atom.arguments[0] == "config"}
        self.assertIn(("address", "/fixture/profile/profile.json"), config)
        self.assertIn(("presence", "present"), config)
        self.assertIn(("kind", "symlink"), config)
        self.assertIn("/fixture/profile/profile.json · present · symlink",
                      terminal_where(result))

    def test_absence_and_failure_do_not_become_each_other_or_health(self):
        cases = json.loads(FILESYSTEM_CORPUS.read_text())["cases"]
        by_name = {case["name"]: case for case in cases}
        rendered = {}
        for name in ("known-absence", "inspection-failure"):
            carrier = {"schema": "tap.filesystem-sensor-receipts/v1",
                       "receipts": {"config": by_name[name]["receipt"]}}
            result = with_filesystem_observations(
                public_where_result(RAW), public_filesystem_result(carrier))
            rendered[name] = terminal_where(result)
            self.assertNotIn("healthy", json.dumps(result))
            self.assertNotIn("valid", json.dumps(result))
        self.assertIn(" · absent", rendered["known-absence"])
        self.assertIn(" · ? inspection failed", rendered["inspection-failure"])

    def test_saved_raw_result_becomes_versioned_address_only_result(self):
        result = public_where_result(RAW)
        self.assertEqual(result["schema"], "tap.where-result/v1")
        self.assertEqual(len(result["addresses"]), 15)
        self.assertEqual(result["addresses"]["profile"],
                         {"knowledge": "known", "value": "/fixture/profile"})
        component = result["addresses"]["component_launch_agent"]
        self.assertEqual(component["knowledge"], "unknown")
        self.assertEqual(component["reason"], "not_observed")
        self.assertNotIn("present", json.dumps(result))
        self.assertNotIn("healthy", json.dumps(result))

    def test_material_owns_location_group_role_owner_label_and_order(self):
        material = copy.deepcopy(load_material())
        config = next(item for item in material["locations"] if item["id"] == "config")
        config.update({"label": "settings", "section": "runtime", "order": 3,
                       "role": "alternate-config", "owner": "fixture"})
        result = public_where_result(RAW)
        changed = next(atom for atom in project_where(result, material).meanings
                       if atom.arguments[0] == "config")
        attributes = dict(changed.arguments[4])
        self.assertEqual((attributes["label"], attributes["section"], attributes["role"],
                          attributes["owner"], changed.arguments[2]),
                         ("settings", "runtime", "alternate-config", "fixture",
                          "/fixture/profile/profile.json"))
        text = terminal_where(result, material=material)
        self.assertIn("settings", text)
        self.assertIn("/fixture/profile/profile.json", text)

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

    def test_carrier_declaration_order_does_not_change_public_result(self):
        adapter = copy.deepcopy(load_carrier_adapter())
        baseline = public_where_result(RAW, adapter)
        adapter["observations"]["addresses"].reverse()
        self.assertEqual(public_where_result(dict(reversed(list(RAW.items()))), adapter),
                         baseline)

    def test_configured_component_addresses_are_known_without_health_claims(self):
        raw = {**RAW,
               "component_launch_agent": "/fixture/component.plist",
               "component_log": "/fixture/profile/logs/components.log",
               "handler_logs": "/fixture/profile/logs/handlers"}
        result = public_where_result(raw)
        components = [result["addresses"][name] for name in
                      ("component_launch_agent", "component_log", "handler_logs")]
        self.assertTrue(all(item["knowledge"] == "known" for item in components))

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

    def test_result_validation_rejects_shape_or_address_drift(self):
        result = public_where_result(RAW)
        changed = copy.deepcopy(result)
        changed["addresses"]["invented"] = {"knowledge": "known", "value": "/tmp"}
        with self.assertRaisesRegex(ProjectionError, "invalid where result"):
            validate_where_result(changed)
        changed = copy.deepcopy(result)
        changed["addresses"]["profile"]["value"] = None
        with self.assertRaisesRegex(ProjectionError, "invalid known where address"):
            validate_where_result(changed)

    def test_separate_adapter_supports_a_second_physical_carrier(self):
        nested = {"metadata": {"inspection_errors": {}}, "payload": RAW}
        adapter = load_carrier_adapter(NESTED_ADAPTER)
        self.assertEqual(public_where_result(nested, adapter), public_where_result(RAW))

    def test_semantic_material_has_no_physical_carrier_paths(self):
        for material in (load_material(), load_material(PRESENCE_MATERIAL)):
            self.assertNotIn('"source"', json.dumps(material))
            self.assertNotIn('"path"', json.dumps(material))

    def test_projection_does_not_branch_on_the_config_identity(self):
        source = (ROOT / "tap_core/where_view.py").read_text()
        self.assertNotIn('== "config"', source)
        self.assertNotIn("== 'config'", source)

    def test_adapter_and_material_have_separate_schema_and_identity(self):
        adapter = load_carrier_adapter()
        material = load_material()
        self.assertNotEqual(adapter["schema"], material["schema"])
        self.assertNotEqual(adapter["id"], material["id"])
        self.assertEqual(adapter["output_schema"], material["input_schema"])

    def test_invalid_adapter_fails_before_reading_the_carrier(self):
        adapter = copy.deepcopy(load_carrier_adapter())
        adapter["observations"]["addresses"][1]["name"] = "profile"
        with self.assertRaisesRegex(ProjectionError, "invalid where carrier observation"):
            public_where_result(object(), adapter)

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
        self.assertEqual(semantic["schema"], "tap.where-result/v2")
        self.assertEqual(semantic["filesystem"]["config"]["presence"], "present")
        self.assertEqual(semantic["filesystem"]["config"]["kind"], "regular")
        code, terminal, error = self.invoke("--output", "terminal", "--color", "never")
        self.assertEqual((code, error), (0, ""))
        self.assertIn(str(self.root / "profile.json"), terminal)
        self.assertIn(" · present · regular", terminal)
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


class FilesystemSensorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="tap-where-sensor-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def test_lstat_distinguishes_regular_symlink_other_and_absence(self):
        regular = self.root / "regular"
        regular.write_text("value")
        link = self.root / "link"
        link.symlink_to(regular)
        self.assertEqual(inspect_path(regular)["kind"], "regular")
        self.assertEqual(inspect_path(link)["kind"], "symlink")
        self.assertEqual(inspect_path(self.root)["kind"], "other")
        self.assertEqual(inspect_path(self.root / "missing")["outcome"], "absent")

    def test_permission_failure_remains_a_failed_receipt(self):
        denied = PermissionError(13, "permission denied", str(self.root / "denied"))
        with patch("tap_core.filesystem_sensor.os.lstat", side_effect=denied):
            receipt = inspect_path(self.root / "denied")
        self.assertEqual(receipt["outcome"], "failed")
        self.assertEqual(receipt["errno"], 13)

    def test_capture_plan_is_data_and_calls_only_its_declared_target(self):
        seen = []
        receipts = capture_filesystem_receipts(
            RAW, load_filesystem_adapter(),
            sensor=lambda path: (seen.append(path) or {
                "outcome": "present", "kind": "regular", "errno": None, "message": ""}))
        self.assertEqual(seen, [RAW["config"]])
        self.assertEqual(set(receipts["receipts"]), {"config"})


if __name__ == "__main__":
    unittest.main()
