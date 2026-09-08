from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from tap_core.cli import main
from tap_core.projection import (Atom, Derivation, Document, ProjectionConflict,
                                 ProjectionError, Slot, document_from_claims,
                                 evaluate_rules, render_terminal,
                                 render_terminal_result, select_candidates)
from tap_core.status_view import (project_status, public_status_result,
                                  terminal_status, validate_status_result)


FIXTURES = Path(__file__).parents[1] / "contracts/status-result/v1/fixtures"


def snapshot(**changes):
    value = {
        "profile": "/fixture/profile", "routing": "system", "service_loaded": True,
        "pid": 123, "port_owned": True, "port_open": True,
        "network_recovery_pending": False, "system_proxy_verified": False,
        "inspection_errors": {}, "capture": {"healthy": True},
        "bridge": {"healthy": True}, "components": {"healthy": True},
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

    def test_unknown_is_not_stopped_and_drift_is_not_direct(self):
        unknown = states(snapshot(service_loaded=None, inspection_errors={"service_loaded": "denied"}))
        drift = states(snapshot(network_recovery_pending=True, system_proxy_verified=False))
        self.assertEqual(unknown["runtime"][0], "unknown")
        self.assertEqual(drift["routing"][0], "recovery-required")


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
        self.assertEqual(result.text, "tap           ● up\nbrowser/apps  ○ direct")
        self.assertEqual([item["slot"] for item in result.omissions],
                         ["runtime-attachment", "routing-attachment"])
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


class StatusCliTests(unittest.TestCase):
    def invoke(self, raw, *arguments):
        out, err = io.StringIO(), io.StringIO()
        with patch("tap_core.cli.platform.system", return_value="Darwin"), \
                patch("tap_core.cli.Profile.load"), patch("tap_core.cli.status", return_value=raw), \
                redirect_stdout(out), redirect_stderr(err):
            code = main(["--profile", "/fixture/profile", "status", *arguments])
        return code, out.getvalue(), err.getvalue()

    def test_unflagged_output_is_byte_for_byte_the_existing_raw_json(self):
        raw = snapshot()
        code, out, err = self.invoke(raw)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, json.dumps(raw, indent=2) + "\n")

    def test_explicit_output_modes_width_and_color(self):
        raw = snapshot()
        code, semantic, _ = self.invoke(raw, "--output", "semantic-json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(semantic)["schema"], "tap.status-result/v1")
        code, narrow, _ = self.invoke(raw, "--output", "terminal", "--width", "24", "--color", "never")
        self.assertEqual(code, 0)
        self.assertEqual(narrow, "tap           ● up\nbrowser/apps  ○ direct\n")
        code, colored, _ = self.invoke(raw, "--output", "terminal", "--color", "always")
        self.assertEqual(code, 0)
        self.assertIn("\x1b[32m", colored)

    def test_presentation_failure_does_not_poison_later_raw_output(self):
        raw = snapshot()
        with patch("tap_core.status_view.terminal_status", side_effect=ProjectionError("forced failure")):
            code, _, err = self.invoke(raw, "--output", "terminal")
        self.assertEqual(code, 1)
        self.assertIn("forced failure", err)
        code, out, err = self.invoke(raw)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, json.dumps(raw, indent=2) + "\n")


if __name__ == "__main__":
    unittest.main()
