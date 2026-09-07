import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tap_core.cli import main
from tap_core.presentation import (
    RENDER_DOCUMENT,
    TERMINAL_PLAN,
    assess_routing,
    assess_runtime,
    compose_status_meaning,
    layout_visual_document,
    normalize_routing_observations,
    normalize_status_observations,
    project_visual_semantics,
    resolve_terminal_notation,
    serialize_terminal_plan,
    status_artifacts,
    status_terminal,
    verbalize_status_meaning,
)
from tap_core.runtime import Profile


def snapshot(**overrides):
    value = {
        "profile": "/fixture/profile",
        "port": 18999,
        "service_loaded": True,
        "pid": 123,
        "port_owned": True,
        "port_open": True,
        "routing": "system",
        "network_recovery_pending": False,
        "system_proxy_verified": False,
        "inspection_errors": {},
    }
    value.update(overrides)
    return value


class PresentationTests(unittest.TestCase):
    def test_complete_path_reaches_two_legacy_shaped_rows(self):
        self.assertEqual(
            status_terminal(snapshot()),
            "tap           ● up · PID 123\n"
            "browser/apps  ○ direct   (tap on)",
        )

    def test_runtime_claim_precedes_communicative_meaning(self):
        observed = normalize_status_observations(snapshot())
        self.assertEqual(observed["observations"]["pid"], {
            "knowledge": "known", "value": 123})
        runtime = assess_runtime(observed)
        self.assertEqual(runtime["claim"], {
            "subject": "tap.runtime",
            "predicate": "operational-state",
            "value": "running",
            "reason": "service_owns_listener",
            "process_id": 123,
        })

        routing = assess_routing(normalize_routing_observations(snapshot()))
        meaning = compose_status_meaning(runtime, routing)
        self.assertEqual(meaning["items"][0]["attachments"], [{
            "relation": "supporting-evidence",
            "concept": "process-id",
            "value": 123,
        }])

    def test_suggested_action_acquires_parentheses_only_in_terminal_notation(self):
        artifacts = status_artifacts(snapshot())
        routing_meaning = artifacts["meaning"]["items"][1]
        self.assertEqual(routing_meaning["attachments"], [{
            "relation": "suggested-action",
            "concept": "command-invocation",
            "value": ["tap", "on"],
        }])
        routing_message = artifacts["messages"]["messages"][1]
        self.assertEqual(routing_message["attachments"], [{
            "relation": "suggested-action", "text": "tap on"}])
        routing_visual = artifacts["visual"]["items"][1]
        self.assertEqual(routing_visual["attachments"][0]["connection"], "aside")
        self.assertEqual(routing_visual["attachments"][0]["enclosure"], "parenthetical")
        terminal_segment = artifacts["terminal_plan"]["lines"][1][-1]
        self.assertEqual(terminal_segment["text"], "   (tap on)")

    def test_notation_does_not_leak_into_earlier_semantic_artifacts(self):
        artifacts = status_artifacts(snapshot())
        for name in (
                "status_observations", "runtime_assessment", "routing_observations",
                "routing_assessment", "meaning", "messages", "visual", "render_document"):
            encoded = json.dumps(artifacts[name], ensure_ascii=False)
            with self.subTest(artifact=name):
                for notation in ("●", "○", "✗", "⚠", "·", "(", ")"):
                    self.assertNotIn(notation, encoded)
        encoded_plan = json.dumps(artifacts["terminal_plan"], ensure_ascii=False)
        self.assertIn("●", encoded_plan)
        self.assertIn("○", encoded_plan)
        self.assertIn("·", encoded_plan)
        self.assertIn("(tap on)", encoded_plan)

    def test_inspection_failure_remains_unknown_through_both_chains(self):
        raw = snapshot(pid=None, inspection_errors={"pid": "permission denied"})
        artifacts = status_artifacts(raw)
        self.assertEqual(artifacts["status_observations"]["observations"]["pid"], {
            "knowledge": "unknown",
            "reason": "inspection_failed",
            "message": "permission denied",
        })
        self.assertEqual(artifacts["runtime_assessment"]["claim"]["value"], "unknown")
        self.assertEqual(
            status_terminal(raw).splitlines()[0],
            "tap           ? unknown · inspection incomplete",
        )

    def test_routing_branch_preserves_distinct_domain_meanings(self):
        cases = [
            ({"routing": "system", "network_recovery_pending": False,
              "system_proxy_verified": False},
             "direct", "browser/apps  ○ direct   (tap on)"),
            ({"routing": "system", "network_recovery_pending": True,
              "system_proxy_verified": True},
             "capturing", "browser/apps  ● capturing · system proxy"),
            ({"routing": "explicit", "network_recovery_pending": False,
              "system_proxy_verified": "not_used"},
             "client_opt_in", "browser/apps  ○ explicit · clients opt in"),
            ({"routing": "system", "network_recovery_pending": True,
              "system_proxy_verified": False},
             "recovery_required", "browser/apps  ⚠ routing drift   (tap off)"),
            ({"routing": "system", "network_recovery_pending": False,
              "system_proxy_verified": True},
             "unowned_route", "browser/apps  ⚠ capturing · recovery snapshot missing"),
        ]
        for fields, state, rendered in cases:
            with self.subTest(fields=fields):
                raw = snapshot(**fields)
                assessment = assess_routing(normalize_routing_observations(raw))
                self.assertEqual(assessment["claim"]["value"], state)
                self.assertEqual(status_terminal(raw).splitlines()[1], rendered)

    def test_routing_inspection_failure_does_not_claim_direct(self):
        raw = snapshot(system_proxy_verified=None,
                       inspection_errors={"system_proxy_verified": "permission denied"})
        artifacts = status_artifacts(raw)
        self.assertEqual(artifacts["routing_assessment"]["claim"]["value"], "unknown")
        self.assertEqual(
            status_terminal(raw).splitlines()[1],
            "browser/apps  ? unknown · inspection incomplete",
        )

    def test_each_expression_boundary_is_independently_callable(self):
        raw = snapshot()
        runtime = assess_runtime(normalize_status_observations(raw))
        routing = assess_routing(normalize_routing_observations(raw))
        meaning = compose_status_meaning(runtime, routing)
        messages = verbalize_status_meaning(meaning)
        visual = project_visual_semantics(messages)
        document = layout_visual_document(visual)
        plan = resolve_terminal_notation(document)
        self.assertEqual(document["schema"], RENDER_DOCUMENT)
        self.assertEqual(plan["schema"], TERMINAL_PLAN)
        self.assertEqual(serialize_terminal_plan(plan), status_terminal(raw))
        self.assertEqual(json.loads(json.dumps(status_artifacts(raw))), status_artifacts(raw))

    def test_terminal_serialization_applies_style_after_notation_resolution(self):
        plan = {
            "schema": TERMINAL_PLAN,
            "lines": [[
                {"text": "anything "},
                {"text": "●", "ansi": "\033[32m", "role": "indicator"},
                {"text": " ready"},
            ]],
        }
        self.assertEqual(serialize_terminal_plan(plan), "anything ● ready")
        self.assertEqual(
            serialize_terminal_plan(plan, color=True),
            "anything \033[32m●\033[0m ready",
        )

    def test_cli_keeps_json_default_and_offers_explicit_terminal_path(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = Profile(Path(directory), "/fixture/backend", 18999, "system",
                              "http://example.test", [])
            raw = snapshot(profile=str(profile.root))
            with patch("tap_core.cli.platform.system", return_value="Darwin"), \
                    patch("tap_core.cli.Profile.load", return_value=profile), \
                    patch("tap_core.cli.MacOS"), \
                    patch("tap_core.cli.status", return_value=raw):
                machine = io.StringIO()
                with contextlib.redirect_stdout(machine):
                    self.assertEqual(main(["--profile", directory, "status"]), 0)
                self.assertEqual(json.loads(machine.getvalue()), raw)
                human = io.StringIO()
                with contextlib.redirect_stdout(human):
                    self.assertEqual(main([
                        "--profile", directory, "status", "--output", "terminal",
                        "--color", "never",
                    ]), 0)
                self.assertEqual(
                    human.getvalue(),
                    "tap           ● up · PID 123\n"
                    "browser/apps  ○ direct   (tap on)\n",
                )


if __name__ == "__main__":
    unittest.main()
