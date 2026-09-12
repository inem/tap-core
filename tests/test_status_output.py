from contextlib import redirect_stderr, redirect_stdout
import copy
from dataclasses import replace
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from tap_core.cli import main, parser
from tap_core.projection import (Atom, Derivation, Document, ProjectionConflict,
                                 ProjectionError, Slot, document_from_claims,
                                 evaluate_rules, render_terminal,
                                 render_terminal_result, select_candidates)
from tap_core.status_view import (project_status, public_status_result,
                                  terminal_status, validate_status_result,
                                  load_carrier_adapter, load_material, observe,
                                  observe_collection)


FIXTURES = Path(__file__).parents[1] / "contracts/status-result/v4/fixtures"
CARRIER_FIXTURES = Path(__file__).parent / "fixtures/status-carriers"


def snapshot(**changes):
    value = {
        "profile": "/fixture/profile", "routing": "system", "service_loaded": True,
        "pid": 123, "port_owned": True, "port_open": True,
        "network_recovery_pending": False, "system_proxy_verified": False,
        "inspection_errors": {},
        "capture": {"available": True, "current_process": True, "healthy": True},
        "bridge": {"configured": False, "healthy": True},
        "components": {"configured": False, "healthy": True},
    }
    value.update(changes)
    return value


def states(value):
    return {atom.arguments[0]: (atom.arguments[2], atom.arguments[3])
            for atom in project_status(public_status_result(value)).meanings}


class StatusContractTests(unittest.TestCase):
    def test_checked_in_fixtures_match_both_projections(self):
        for path in sorted(FIXTURES.glob("*.json")):
            with self.subTest(path=path.name):
                fixture = json.loads(path.read_text())
                semantic = public_status_result(fixture["raw_snapshot"])
                self.assertEqual(semantic, fixture["semantic_result"])
                validate_status_result(semantic)
                self.assertEqual(terminal_status(semantic), fixture["terminal"]["width_80"])

    def test_runtime_state_matrix(self):
        cases = [
            ({}, ("running", "service_owns_listener")),
            ({"service_loaded": False, "pid": None, "port_owned": False, "port_open": False},
             ("stopped", "service_absent_and_port_closed")),
            ({"port_owned": False, "port_open": True},
             ("port-conflict", "listener_not_owned_by_profile")),
            ({"port_open": False}, ("broken", "service_has_no_listener")),
            ({"port_owned": None, "inspection_errors": {"port_owned": "denied"}},
             ("unknown", "listener_ownership_unknown")),
            ({"service_loaded": False, "pid": 123, "port_owned": True, "port_open": True},
             ("unknown", "inconsistent_runtime_observations")),
        ]
        for changes, expected in cases:
            with self.subTest(changes=changes):
                self.assertEqual(states(snapshot(**changes))["runtime"], expected)
        for field in ("service_loaded", "pid", "port_open"):
            with self.subTest(unknown=field):
                self.assertEqual(states(snapshot(**{field: None, "inspection_errors": {field: "denied"}}))["runtime"],
                                 ("unknown", "inspection_incomplete"))

    def test_routing_state_matrix(self):
        cases = [
            ({"routing": "explicit", "network_recovery_pending": False,
              "system_proxy_verified": "not_used"}, ("client-opt-in", "system_proxy_not_managed")),
            ({"routing": "explicit", "network_recovery_pending": True,
              "system_proxy_verified": "not_used"}, ("recovery-required", "unexpected_recovery_snapshot")),
            ({"network_recovery_pending": True, "system_proxy_verified": True},
             ("capturing", "owned_system_proxy_verified")),
            ({"network_recovery_pending": False, "system_proxy_verified": False},
             ("direct", "system_proxy_disabled")),
            ({"network_recovery_pending": True, "system_proxy_verified": False},
             ("recovery-required", "owned_system_proxy_drifted")),
            ({"network_recovery_pending": False, "system_proxy_verified": True},
             ("unowned-route", "recovery_snapshot_missing")),
            ({"routing": "explicit", "network_recovery_pending": False,
              "system_proxy_verified": False}, ("unknown", "inconsistent_routing_observations")),
        ]
        for changes, expected in cases:
            with self.subTest(changes=changes):
                self.assertEqual(states(snapshot(**changes))["routing"], expected)
        for field in ("routing", "network_recovery_pending", "system_proxy_verified"):
            with self.subTest(unknown=field):
                self.assertEqual(states(snapshot(**{field: None, "inspection_errors": {field: "denied"}}))["routing"],
                                 ("unknown", "inspection_incomplete"))

    def test_capture_state_matrix(self):
        cases = [
            ({"available": True, "current_process": True, "healthy": True},
             ("ready", "current_writer_healthy")),
            ({"available": False, "current_process": False, "healthy": False},
             ("absent", "health_record_absent")),
            ({"available": True, "current_process": False, "healthy": False},
             ("stale", "health_record_not_current")),
            ({"available": True, "current_process": True, "healthy": False},
             ("degraded", "current_writer_unhealthy")),
            ({"available": True, "current_process": False, "healthy": True},
             ("unknown", "inconsistent_capture_observations")),
        ]
        for capture, expected in cases:
            with self.subTest(capture=capture):
                self.assertEqual(states(snapshot(capture=capture))["capture"], expected)
        unknown = states(snapshot(
            capture={"available": None, "current_process": None, "healthy": None},
            inspection_errors={"capture": "denied"}))
        self.assertEqual(unknown["capture"], ("unknown", "inspection_incomplete"))

    def test_capture_meaning_preserves_all_three_writer_observations(self):
        projection = project_status(public_status_result(snapshot()))
        meaning = next(atom for atom in projection.meanings if atom.arguments[0] == "capture")
        candidate = projection.provenance[meaning].warrants[0]
        warrants = projection.provenance[candidate].warrants
        self.assertEqual({atom.arguments[1] for atom in warrants},
                         {"available", "current_process", "healthy"})

    def test_bridge_state_matrix_does_not_claim_hub_liveness(self):
        cases = [
            ({"configured": False, "healthy": True},
             ("absent", "bridge_not_configured")),
            ({"configured": True, "enabled": False, "healthy": True,
              "hub_liveness": "not_checked"},
             ("disabled", "disabled_snapshot_applied")),
            ({"configured": True, "enabled": True, "healthy": True,
              "hub_liveness": "not_checked"},
             ("applied", "enabled_snapshot_applied_hub_unchecked")),
            ({"configured": True, "enabled": True, "healthy": False,
              "hub_liveness": "not_checked"},
             ("drifted", "running_runtime_snapshot_mismatch")),
            ({"configured": False, "healthy": False},
             ("unknown", "inconsistent_bridge_observations")),
        ]
        for bridge, expected in cases:
            with self.subTest(bridge=bridge):
                self.assertEqual(states(snapshot(bridge=bridge))["bridge"], expected)
        unknown = states(snapshot(
            bridge={"configured": False, "healthy": None},
            inspection_errors={"bridge": "denied"}))
        self.assertEqual(unknown["bridge"], ("unknown", "inspection_incomplete"))

    def test_bridge_meaning_preserves_snapshot_scope_and_unchecked_liveness(self):
        bridge = {"configured": True, "enabled": True, "healthy": True,
                  "hub_liveness": "not_checked"}
        projection = project_status(public_status_result(snapshot(bridge=bridge)))
        meaning = next(atom for atom in projection.meanings if atom.arguments[0] == "bridge")
        self.assertEqual(meaning.arguments[2], "applied")
        candidate = projection.provenance[meaning].warrants[0]
        warrants = projection.provenance[candidate].warrants
        self.assertEqual({atom.arguments[1] for atom in warrants},
                         {"configured", "enabled", "healthy", "hub_liveness"})
        self.assertIn("hub unchecked", render_terminal(projection.document))

    def test_component_controller_state_matrix(self):
        ready = {"configured": True, "healthy": True, "current_process": True,
                 "phase": "ready", "hub_pid": 4321, "error": None, "readers": {}}
        cases = [
            ({"configured": False, "healthy": True},
             ("absent", "components_not_configured")),
            ({"configured": True, "healthy": False, "phase": "absent"},
             ("inactive", "controller_observation_absent")),
            (dict(ready, healthy=False, phase="starting"),
             ("starting", "controller_starting")),
            (ready, ("ready", "page_control_plane_healthy")),
            (dict(ready, healthy=False),
             ("degraded", "page_control_plane_unhealthy")),
            (dict(ready, healthy=False, current_process=False),
             ("stale", "controller_state_not_current")),
            (dict(ready, healthy=False, current_process=False, phase="failed",
                  error="Hub unavailable"),
             ("failed", "controller_reported_failure")),
        ]
        for components, expected in cases:
            with self.subTest(components=components):
                self.assertEqual(states(snapshot(components=components))["components"], expected)
        unknown = states(snapshot(
            components={"configured": True, "healthy": None},
            inspection_errors={"components": "denied"}))
        self.assertEqual(unknown["components"], ("unknown", "inspection_incomplete"))

    def test_named_reader_collection_becomes_stable_independent_meanings(self):
        components = {"configured": True, "healthy": True, "current_process": True,
                      "phase": "ready", "hub_pid": 4321, "error": None,
                      "readers": {
                          "zeta": {"healthy": False, "phase": "backoff", "failures": 1,
                                   "error": "ReaderError: fixture"},
                          "alpha": {"healthy": True, "phase": "waiting",
                                    "progress": {"processed": 2}, "error": None},
                      }}
        result = public_status_result(snapshot(components=components))
        self.assertEqual([(item["name"], item["ordinal"])
                          for item in result["component_readers"]["items"]],
                         [("alpha", 1), ("zeta", 2)])
        projection = project_status(result)
        meanings = {atom.arguments[0]: atom.arguments[2] for atom in projection.meanings}
        self.assertEqual(meanings["reader:alpha"], "waiting")
        self.assertEqual(meanings["reader:zeta"], "backoff")
        rendered = render_terminal(projection.document)
        self.assertLess(rendered.index("  alpha"), rendered.index("  zeta"))
        by_id = {slot.identifier: slot for slot in projection.document.slots}
        self.assertEqual(by_id["reader-alpha-line"].parent, "components-section")
        self.assertEqual(by_id["reader-zeta-line"].order, 2)

        for subject in ("reader:alpha", "reader:zeta"):
            meaning = next(atom for atom in projection.meanings if atom.arguments[0] == subject)
            candidate = projection.provenance[meaning].warrants[0]
            self.assertEqual(len(projection.provenance[candidate].warrants), 5)

    def test_unapplied_bridge_is_refined_by_runtime_before_presentation(self):
        bridge = {"configured": True, "enabled": True, "healthy": False,
                  "hub_liveness": "not_checked"}
        cases = [
            ({}, ("drifted", "running_runtime_snapshot_mismatch")),
            ({"service_loaded": False, "pid": None, "port_owned": False,
              "port_open": False}, ("inactive", "runtime_stopped")),
            ({"port_open": False}, ("blocked", "broken_runtime")),
            ({"port_owned": False, "port_open": True},
             ("blocked", "runtime_port_conflict")),
            ({"service_loaded": None,
              "inspection_errors": {"service_loaded": "denied"}},
             ("unverified", "runtime_inspection_incomplete")),
        ]
        for changes, expected in cases:
            with self.subTest(changes=changes):
                projection = project_status(public_status_result(
                    snapshot(bridge=bridge, **changes)))
                meanings = {atom.arguments[0]: (atom.arguments[2], atom.arguments[3])
                            for atom in projection.meanings}
                self.assertEqual(meanings["bridge"], expected)
                self.assertNotIn("unapplied", render_terminal(projection.document))

    def test_unknown_is_not_stopped_and_drift_is_not_direct(self):
        unknown = states(snapshot(service_loaded=None, inspection_errors={"service_loaded": "denied"}))
        drift = states(snapshot(network_recovery_pending=True, system_proxy_verified=False))
        self.assertEqual(unknown["runtime"][0], "unknown")
        self.assertEqual(drift["routing"][0], "recovery-required")

    def test_owned_route_and_unusable_runtime_compose_into_a_required_alert(self):
        routed = {"network_recovery_pending": True, "system_proxy_verified": True}
        for changes in (
                {"service_loaded": False, "pid": None, "port_owned": False,
                 "port_open": False},
                {"port_open": False}):
            with self.subTest(changes=changes):
                projection = project_status(public_status_result(snapshot(**routed, **changes)))
                traffic = next(atom for atom in projection.meanings
                               if atom.arguments[:2] == ("status", "traffic"))
                self.assertEqual(traffic.arguments[2], "broken")
                candidate = projection.provenance[traffic].warrants[0]
                warrants = projection.provenance[candidate].warrants
                self.assertEqual({atom.arguments[0] for atom in warrants}, {"runtime", "routing"})
                self.assertIn("traffic broken", render_terminal(projection.document))
                with self.assertRaisesRegex(ProjectionError, "required content exceeds width"):
                    render_terminal(projection.document, 24)

    def test_owned_route_to_conflicting_listener_is_unowned_not_broken(self):
        raw = snapshot(network_recovery_pending=True, system_proxy_verified=True,
                       port_owned=False, port_open=True)
        projection = project_status(public_status_result(raw))
        traffic = next(atom for atom in projection.meanings
                       if atom.arguments[:2] == ("status", "traffic"))
        self.assertEqual(traffic.arguments[2], "unowned")
        rendered = render_terminal(projection.document)
        self.assertIn("traffic routed to unowned listener", rendered)
        self.assertNotIn("traffic broken", rendered)

    def test_owned_route_preserves_unknown_runtime_as_unverified(self):
        raw = snapshot(network_recovery_pending=True, system_proxy_verified=True,
                       port_owned=None, inspection_errors={"port_owned": "denied"})
        projection = project_status(public_status_result(raw))
        traffic = next(atom for atom in projection.meanings
                       if atom.arguments[:2] == ("status", "traffic"))
        self.assertEqual(traffic.arguments[2], "unverified")
        self.assertIn("capture unverified", render_terminal(projection.document))

    def test_direct_route_has_no_cross_section_alert(self):
        raw = snapshot(service_loaded=False, pid=None, port_owned=False, port_open=False)
        projection = project_status(public_status_result(raw))
        self.assertFalse(any(atom.arguments[:2] == ("status", "traffic")
                             for atom in projection.meanings))
        self.assertEqual(render_terminal(projection.document).count("\n"), 4)

    def test_absent_field_is_not_observed_instead_of_known_null(self):
        raw = snapshot()
        del raw["port_owned"]
        semantic = public_status_result(raw)
        self.assertEqual(semantic["runtime"]["port_owned"], {
            "knowledge": "unknown", "reason": "not_observed",
            "message": "observation was not supplied",
        })
        validate_status_result(semantic)
        self.assertEqual(states(raw)["runtime"], ("unknown", "listener_ownership_unknown"))
        semantic["runtime"]["port_owned"]["reason"] = "invented"
        with self.assertRaisesRegex(ProjectionError, "invalid unknown"):
            validate_status_result(semantic)

    def test_same_free_operation_reads_flat_and_nested_snapshot_paths(self):
        flat = {"path": ["service_loaded"], "error": "service_loaded"}
        nested = {"path": ["capture", "healthy"], "error": "capture"}
        raw = snapshot()
        self.assertEqual(observe(flat, raw, {}), {"knowledge": "known", "value": True})
        self.assertEqual(observe(nested, raw, {}), {"knowledge": "known", "value": True})

    def test_nested_observation_is_data_and_uses_its_group_error_owner(self):
        adapter = copy.deepcopy(load_carrier_adapter())
        adapter["observations"]["capture_probe"] = [{
            "name": "healthy",
            "source": {"path": ["capture", "healthy"], "error": "capture"},
        }]
        known = public_status_result(snapshot(), adapter)
        self.assertEqual(known["capture_probe"]["healthy"], {
            "knowledge": "known", "value": True,
        })
        failed = public_status_result(
            snapshot(inspection_errors={"capture": "cannot read health"}), adapter)
        self.assertEqual(failed["capture_probe"]["healthy"], {
            "knowledge": "unknown", "reason": "inspection_failed",
            "message": "cannot read health",
        })
        absent = public_status_result(snapshot(capture={}), adapter)
        self.assertEqual(absent["capture_probe"]["healthy"]["reason"], "not_observed")

    def test_observation_source_shape_is_validated_at_the_operation_boundary(self):
        for invalid in ({"path": [], "error": "capture"},
                        {"path": ["capture", "healthy"]},
                        {"path": "capture.healthy", "error": "capture"}):
            with self.subTest(invalid=invalid), \
                    self.assertRaisesRegex(ProjectionError, "invalid status observation source"):
                observe(invalid, snapshot(), {})

    def test_free_collection_operation_only_normalizes_shape_and_order(self):
        spec = {
            "path": ["components", "readers"], "error": "components",
            "max_items": 8,
            "fields": [{"name": "phase", "path": ["phase"]}],
        }
        raw = snapshot(components={"readers": {
            "zeta": {"phase": "failed"}, "alpha": {"phase": "waiting"}}})
        result = observe_collection(spec, raw, {})
        self.assertEqual([(item["name"], item["ordinal"])
                          for item in result["items"]], [("alpha", 1), ("zeta", 2)])
        self.assertEqual(result["items"][0]["observations"]["phase"]["value"], "waiting")
        failed = observe_collection(spec, snapshot(), {"components": "denied"})
        self.assertEqual(failed["reason"], "inspection_failed")
        with self.assertRaisesRegex(ProjectionError, "named map"):
            observe_collection(spec, snapshot(components={"readers": []}), {})
        bounded = dict(spec, max_items=1)
        with self.assertRaisesRegex(ProjectionError, "declared bound"):
            observe_collection(bounded, raw, {})


class CarrierMaterialBoundaryTests(unittest.TestCase):
    def nested_snapshot(self, raw):
        payload = {key: value for key, value in raw.items()
                   if key not in ("profile", "inspection_errors")}
        return {"metadata": {"profile": raw["profile"],
                             "inspection_errors": raw["inspection_errors"]},
                "payload": payload}

    def test_two_physical_carriers_produce_the_same_public_material(self):
        raw = snapshot(components={
            "configured": True, "healthy": True, "current_process": True,
            "phase": "ready", "hub_pid": 4321, "error": None,
            "readers": {"reader-a": {"healthy": True, "phase": "waiting",
                                      "progress": {"processed": 2}, "error": None}},
        })
        nested_adapter = load_carrier_adapter(CARRIER_FIXTURES / "nested.json")
        self.assertEqual(public_status_result(raw),
                         public_status_result(self.nested_snapshot(raw), nested_adapter))

    def test_failure_and_absence_remain_distinct_across_adapter_boundary(self):
        failed = public_status_result(snapshot(
            inspection_errors={"port_owned": "probe timed out"}))
        absent_raw = snapshot()
        del absent_raw["port_owned"]
        absent = public_status_result(absent_raw)
        self.assertEqual(failed["runtime"]["port_owned"]["reason"], "inspection_failed")
        self.assertEqual(absent["runtime"]["port_owned"]["reason"], "not_observed")
        self.assertEqual(failed["runtime"]["port_owned"]["message"], "probe timed out")
        self.assertNotIn("probe timed out", terminal_status(failed))

    def test_material_contains_vocabulary_but_no_physical_carrier_paths(self):
        material = load_material()

        def keys(value):
            if isinstance(value, dict):
                return set(value) | set().union(*(keys(item) for item in value.values()))
            if isinstance(value, list):
                return set().union(*(keys(item) for item in value))
            return set()

        self.assertEqual(material["observations"]["runtime"],
                         ["service_loaded", "pid", "port_owned", "port_open"])
        self.assertTrue({"path", "source", "profile_path", "errors_path"}.isdisjoint(keys(material)))

    def test_adapter_and_material_have_independent_schema_and_identity(self):
        adapter = load_carrier_adapter()
        material = load_material()
        self.assertNotEqual(adapter["schema"], material["schema"])
        self.assertNotEqual(adapter["id"], material["id"])
        self.assertEqual(adapter["output_schema"], material["input_schema"])

    def test_invalid_adapter_is_rejected_before_it_reads_a_snapshot(self):
        adapter = copy.deepcopy(load_carrier_adapter())
        adapter["observations"]["runtime"][1]["name"] = "service_loaded"
        with self.assertRaisesRegex(ProjectionError, "invalid status carrier observation"):
            public_status_result(object(), adapter)

    def test_incompatible_adapter_result_is_rejected_by_material_boundary(self):
        adapter = copy.deepcopy(load_carrier_adapter())
        adapter["output_schema"] = "fixture.status-result/v9"
        with self.assertRaisesRegex(ProjectionError, "unsupported status result"):
            project_status(public_status_result(snapshot(), adapter))


class ProjectionKernelTests(unittest.TestCase):
    def candidate(self, value, rank, operator):
        atom = Atom.from_value(["candidate", "subject", "quality", value, rank, operator, []])
        return atom, Derivation(operator, tuple())

    def test_higher_rank_wins_and_only_winners_are_provenance(self):
        low, low_d = self.candidate("low", 10, "low-rule")
        high, high_d = self.candidate("high", 20, "high-rule")
        selected = select_candidates({low: low_d, high: high_d})
        meaning = next(atom for atom in selected if atom.relation == "meaning")
        self.assertEqual(meaning.arguments[2], "high")
        self.assertEqual(selected[meaning].warrants, (high,))

    def test_equal_rank_incompatible_candidates_are_visible(self):
        first, first_d = self.candidate("first", 20, "a")
        second, second_d = self.candidate("second", 20, "b")
        with self.assertRaises(ProjectionConflict):
            select_candidates({first: first_d, second: second_d})

    def test_real_nested_traversal_and_explicit_omission(self):
        projection = project_status(public_status_result(snapshot()))
        result = render_terminal_result(projection.document, 24)
        self.assertEqual(result.text,
                         "tap           ● up\nbrowser/apps  ○ direct\ncapture       ● ready\n"
                         "bridge        ○ absent\ncontrol       ○ absent")
        self.assertEqual([item["slot"] for item in result.omissions],
                         ["runtime-attachment", "routing-attachment", "capture-attachment",
                          "bridge-attachment", "components-attachment"])
        self.assertTrue(all(item["reason"] == "does-not-fit" for item in result.omissions))
        by_id = {slot.identifier: slot for slot in projection.document.slots}
        self.assertEqual(by_id["runtime-detail"].parent, "runtime-attachment")
        without_root = replace(projection.document,
                               slots=tuple(slot for slot in projection.document.slots if slot.identifier != "status"))
        with self.assertRaisesRegex(ProjectionError, "root"):
            render_terminal(without_root)

    def test_missing_non_root_parent_and_duplicate_order_are_rejected(self):
        projection = project_status(public_status_result(snapshot()))
        slots = list(projection.document.slots)
        detail = next(i for i, slot in enumerate(slots) if slot.identifier == "runtime-detail")
        missing = slots.copy()
        missing[detail] = replace(missing[detail], parent="absent")
        with self.assertRaisesRegex(ProjectionError, "missing parent"):
            render_terminal(replace(projection.document, slots=tuple(missing)))
        duplicate = slots.copy()
        duplicate[detail] = replace(duplicate[detail], order=0)
        with self.assertRaisesRegex(ProjectionError, "duplicate sibling order"):
            render_terminal(replace(projection.document, slots=tuple(duplicate)))

    def test_disconnected_parent_cycle_is_rejected(self):
        projection = project_status(public_status_result(snapshot()))
        slots = list(projection.document.slots)
        first = next(i for i, slot in enumerate(slots) if slot.identifier == "runtime-attachment")
        second = next(i for i, slot in enumerate(slots) if slot.identifier == "runtime-detail")
        slots[first] = replace(slots[first], parent="runtime-detail", optional=False)
        slots[second] = replace(slots[second], parent="runtime-attachment")
        with self.assertRaisesRegex(ProjectionError, "cycle"):
            render_terminal(replace(projection.document, slots=tuple(slots)))

    def test_empty_group_is_rejected(self):
        projection = project_status(public_status_result(snapshot()))
        slots = tuple(slot for slot in projection.document.slots
                      if slot.parent != "runtime-attachment")
        with self.assertRaisesRegex(ProjectionError, "empty group"):
            render_terminal(replace(projection.document, slots=slots))

    def test_document_cannot_silently_drop_a_selected_meaning(self):
        material = copy.deepcopy(load_material())
        material["presentation_rules"] = [
            rule for rule in material["presentation_rules"]
            if rule["id"] != "presentation-routing-02-direct"
        ]
        with self.assertRaisesRegex(ProjectionError, "meaning is not sourced"):
            project_status(public_status_result(snapshot()), material)

    def test_selected_meanings_must_preserve_every_supplied_observation(self):
        material = copy.deepcopy(load_material())
        running = next(rule for rule in material["semantic_rules"]
                       if rule["id"] == "runtime-30-running")
        running["when"] = [pattern for pattern in running["when"]
                           if pattern[2] != "port_owned"]
        with self.assertRaisesRegex(ProjectionError, "observation is not preserved"):
            project_status(public_status_result(snapshot()), material)

    def test_synthetic_non_product_document_uses_same_evaluator_and_renderer(self):
        fact = Atom.from_value(["reading", "forecast", "mild"])
        claims = evaluate_rules({fact: Derivation("fixture", tuple())}, [{
            "id": "describe", "when": [["reading", "$subject", "$value"]],
            "emit": [
                ["slot", "page", None, 0, "group", "document", False, "", None,
                 [["reading", "$subject", "$value"]]],
                ["slot", "line", "page", 0, "group", "line", False, "", None,
                 [["reading", "$subject", "$value"]]],
                ["slot", "value", "line", 0, "leaf", "value", False, "$value", None,
                 [["reading", "$subject", "$value"]]],
            ]
        }])
        self.assertEqual(render_terminal(document_from_claims(claims, "page")), "mild")

    def test_generic_kernel_has_no_product_state_or_action_vocabulary(self):
        source = (Path(__file__).parents[1] / "tap_core/projection.py").read_text()
        for word in ("running", "stopped", "routing", "tap on", "tap off", "PORT STOLEN"):
            self.assertNotIn(word, source)


class MaterialCompositionTests(unittest.TestCase):
    def copied_material(self):
        directory = Path(tempfile.mkdtemp()) / "status"
        shutil.copytree(Path(__file__).parents[1] / "tap_core/data/status", directory)
        self.addCleanup(shutil.rmtree, directory.parent)
        return directory

    def test_fragments_have_distinct_section_and_assembly_owners(self):
        material = load_material()
        self.assertEqual(material["schema"], "tap.internal-status-material/v2")
        self.assertEqual(material["id"], "tap-core.status-terminal-material/v1")
        self.assertEqual(material["input_schema"], "tap.status-result/v4")
        self.assertEqual(set(material["observations"]),
                         {"runtime", "routing", "capture", "bridge", "components"})
        self.assertEqual(set(material["observation_collections"]), {"component_readers"})
        self.assertTrue(material["composition_rules"])
        self.assertEqual(material["document_root"], "status")

    def test_duplicate_rule_is_rejected_before_evaluation(self):
        directory = self.copied_material()
        path = directory / "composition.json"
        fragment = json.loads(path.read_text())
        fragment["composition_rules"][0]["id"] = "runtime-00-fallback"
        path.write_text(json.dumps(fragment))
        with self.assertRaisesRegex(ProjectionError, "duplicate status material rule id"):
            load_material(directory / "manifest.json")

    def test_ambiguous_document_owner_is_rejected_before_evaluation(self):
        directory = self.copied_material()
        path = directory / "composition.json"
        fragment = json.loads(path.read_text())
        fragment["document_root"] = "another-root"
        path.write_text(json.dumps(fragment))
        with self.assertRaisesRegex(ProjectionError, "exactly one document root owner"):
            load_material(directory / "manifest.json")

    def test_fragment_order_does_not_change_carrier_or_projection(self):
        directory = self.copied_material()
        adapter = load_carrier_adapter()
        manifest_path = directory / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["fragments"].reverse()
        manifest_path.write_text(json.dumps(manifest))
        reordered = load_material(manifest_path)
        result = public_status_result(snapshot())
        baseline = project_status(result)
        changed = project_status(result, reordered)
        self.assertEqual(load_carrier_adapter(), adapter)
        self.assertEqual(changed.meanings, baseline.meanings)
        self.assertEqual(changed.document, baseline.document)


class StatusCliTests(unittest.TestCase):
    def test_installed_profile_option_is_hidden_from_ordinary_help(self):
        help_text = parser().format_help()
        self.assertNotIn('--profile', help_text)
        self.assertNotIn('profile-scoped', help_text)

    def invoke(self, raw, *arguments, tty=False):
        class Output(io.StringIO):
            def isatty(self):
                return tty
        out, err = Output(), io.StringIO()
        with patch("tap_core.cli.platform.system", return_value="Darwin"), \
                patch("tap_core.cli.Profile.load"), patch("tap_core.cli.status", return_value=raw), \
                redirect_stdout(out), redirect_stderr(err):
            code = main(["--profile", "/fixture/profile", "status", *arguments])
        return code, out.getvalue(), err.getvalue()

    def test_unflagged_output_is_terminal(self):
        raw = snapshot()
        code, out, err = self.invoke(raw)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, self.invoke(raw, "--output", "terminal")[1])
        self.assertIn("tap", out)

    def test_explicit_raw_output_is_byte_for_byte_the_existing_json(self):
        raw = snapshot()
        code, out, err = self.invoke(raw, "--output", "raw-json")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, json.dumps(raw, indent=2) + "\n")

    def test_explicit_output_modes_width_and_color(self):
        raw = snapshot()
        code, semantic, _ = self.invoke(raw, "--output", "semantic-json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(semantic)["schema"], "tap.status-result/v4")
        code, narrow, _ = self.invoke(raw, "--output", "terminal", "--width", "24", "--color", "never")
        self.assertEqual(code, 0)
        self.assertEqual(narrow,
                         "tap           ● up\nbrowser/apps  ○ direct\ncapture       ● ready\n"
                         "bridge        ○ absent\ncontrol       ○ absent\n")
        code, colored, _ = self.invoke(raw, "--output", "terminal", "--color", "always")
        self.assertEqual(code, 0)
        self.assertIn("\x1b[32m", colored)

    def test_raw_and_semantic_json_ignore_width_color_and_tty(self):
        raw = snapshot()
        baseline_raw = self.invoke(raw, "--output", "raw-json")[1]
        decorated_raw = self.invoke(raw, "--output", "raw-json", "--width", "1", "--color", "always", tty=True)[1]
        self.assertEqual(decorated_raw, baseline_raw)
        baseline_semantic = self.invoke(raw, "--output", "semantic-json")[1]
        decorated_semantic = self.invoke(
            raw, "--output", "semantic-json", "--width", "1", "--color", "always", tty=True)[1]
        self.assertEqual(decorated_semantic, baseline_semantic)
        self.assertNotIn("\x1b[", decorated_semantic)

    def test_color_auto_follows_tty_for_terminal_only(self):
        raw = snapshot()
        plain = self.invoke(raw, "--output", "terminal", "--color", "auto", tty=False)[1]
        colored = self.invoke(raw, "--output", "terminal", "--color", "auto", tty=True)[1]
        self.assertNotIn("\x1b[", plain)
        self.assertIn("\x1b[32m", colored)

    def test_presentation_failure_does_not_poison_later_raw_output(self):
        raw = snapshot()
        with patch("tap_core.status_view.terminal_status", side_effect=ProjectionError("forced failure")):
            code, _, err = self.invoke(raw, "--output", "terminal")
        self.assertEqual(code, 1)
        self.assertIn("forced failure", err)
        code, out, err = self.invoke(raw, "--output", "raw-json")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, json.dumps(raw, indent=2) + "\n")


if __name__ == "__main__":
    unittest.main()
