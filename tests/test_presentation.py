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
    assess_routing_summary,
    assess_status_summary,
    lower_status_line,
    lower_status_lines,
    normalize_routing_observations,
    normalize_status_observations,
    project_routing_line,
    project_status_line,
    render_terminal,
    status_terminal,
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
    def test_complete_path_renders_the_old_status_summary_shape(self):
        self.assertEqual(status_terminal(snapshot()),
                         "tap           ● up · PID 123\n"
                         "browser/apps  ○ direct   (tap on)")
        self.assertEqual(status_terminal(snapshot(service_loaded=False, pid=None,
                                                  port_owned=False, port_open=False)),
                         "tap           ✗ down\n"
                         "browser/apps  ○ direct   (tap on)")

    def test_inspection_failure_remains_unknown_instead_of_becoming_down(self):
        raw = snapshot(pid=None, inspection_errors={"pid": "permission denied"})
        observations = normalize_status_observations(raw)
        self.assertEqual(observations["observations"]["pid"], {
            "knowledge": "unknown", "reason": "inspection_failed", "message": "permission denied"})
        summary = assess_status_summary(observations)
        self.assertEqual(summary["runtime"], {
            "state": "unknown", "reason": "inspection_incomplete"})
        self.assertEqual(project_status_line(summary)["row"]["value"], "unknown")
        self.assertEqual(status_terminal(raw),
                         "tap           ? unknown · inspection incomplete\n"
                         "browser/apps  ○ direct   (tap on)")

    def test_foreign_listener_and_broken_service_are_distinct_claims(self):
        conflict = snapshot(service_loaded=False, pid=None, port_owned=False, port_open=True)
        self.assertEqual(assess_status_summary(normalize_status_observations(conflict))["runtime"], {
            "state": "port_conflict", "reason": "listener_not_owned_by_profile", "port": 18999})
        self.assertTrue(status_terminal(conflict).startswith("tap           ✗ PORT STOLEN\n"))
        broken = snapshot(port_owned=False, port_open=False)
        self.assertTrue(status_terminal(broken).startswith(
            "tap           ✗ broken · service has no listener\n"))

    def test_routing_branch_distinguishes_route_semantics(self):
        cases = [
            ({"routing": "system", "network_recovery_pending": False,
              "system_proxy_verified": False},
             {"state": "direct", "reason": "system_proxy_disabled"},
             "browser/apps  ○ direct   (tap on)"),
            ({"routing": "system", "network_recovery_pending": True,
              "system_proxy_verified": True},
             {"state": "capturing", "reason": "owned_system_proxy_verified"},
             "browser/apps  ● capturing · system proxy"),
            ({"routing": "explicit", "network_recovery_pending": False,
              "system_proxy_verified": "not_used"},
             {"state": "client_opt_in", "reason": "system_proxy_not_managed"},
             "browser/apps  ○ explicit · clients opt in"),
            ({"routing": "system", "network_recovery_pending": True,
              "system_proxy_verified": False},
             {"state": "recovery_required", "reason": "owned_system_proxy_drifted"},
             "browser/apps  ⚠ routing drift   (tap off)"),
        ]
        for fields, claim, rendered in cases:
            with self.subTest(fields=fields):
                observations = normalize_routing_observations(snapshot(**fields))
                summary = assess_routing_summary(observations)
                self.assertEqual(summary["traffic"], claim)
                document = lower_status_line(project_routing_line(summary))
                self.assertEqual(render_terminal(document), rendered)

    def test_routing_inspection_failure_does_not_claim_direct(self):
        raw = snapshot(system_proxy_verified=None,
                       inspection_errors={"system_proxy_verified": "permission denied"})
        summary = assess_routing_summary(normalize_routing_observations(raw))
        self.assertEqual(summary["traffic"], {
            "state": "unknown", "reason": "inspection_incomplete"})
        self.assertEqual(render_terminal(lower_status_line(project_routing_line(summary))),
                         "browser/apps  ? unknown · inspection incomplete")

    def test_independent_views_only_converge_in_render_document(self):
        raw = snapshot()
        runtime_view = project_status_line(
            assess_status_summary(normalize_status_observations(raw)))
        routing_view = project_routing_line(
            assess_routing_summary(normalize_routing_observations(raw)))
        document = lower_status_lines([runtime_view, routing_view])
        self.assertEqual([block["label"] for block in document["blocks"]],
                         ["tap", "browser/apps"])

    def test_renderer_only_understands_document_primitives(self):
        document = {
            "schema": RENDER_DOCUMENT,
            "layout": {"label_width": 8},
            "blocks": [{"type": "status_row", "mark": "warning", "label": "anything",
                        "value": "attention", "detail": "generic detail"}],
        }
        self.assertEqual(render_terminal(document), "anything⚠ attention · generic detail")
        colored = render_terminal(document, color=True)
        self.assertIn("\033[33m⚠\033[0m", colored)
        self.assertNotIn("healthy", colored)

    def test_lowering_is_json_serializable(self):
        summary = assess_status_summary(normalize_status_observations(snapshot()))
        document = lower_status_line(project_status_line(summary))
        self.assertEqual(json.loads(json.dumps(document)), document)

    def test_cli_keeps_json_default_and_offers_explicit_terminal_path(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = Profile(Path(directory), "/fixture/backend", 18999, "explicit",
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
                    self.assertEqual(main(["--profile", directory, "status", "--output", "terminal",
                                           "--color", "never"]), 0)
                self.assertEqual(human.getvalue(),
                                 "tap           ● up · PID 123\n"
                                 "browser/apps  ○ direct   (tap on)\n")


if __name__ == "__main__":
    unittest.main()
