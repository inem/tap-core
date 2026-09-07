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
    assess_status_summary,
    lower_status_line,
    normalize_status_observations,
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
        "inspection_errors": {},
    }
    value.update(overrides)
    return value


class PresentationTests(unittest.TestCase):
    def test_complete_path_renders_the_old_status_summary_shape(self):
        self.assertEqual(status_terminal(snapshot()), "tap           ● up · PID 123")
        self.assertEqual(status_terminal(snapshot(service_loaded=False, pid=None,
                                                  port_owned=False, port_open=False)),
                         "tap           ✗ down")

    def test_inspection_failure_remains_unknown_instead_of_becoming_down(self):
        raw = snapshot(pid=None, inspection_errors={"pid": "permission denied"})
        observations = normalize_status_observations(raw)
        self.assertEqual(observations["observations"]["pid"], {
            "knowledge": "unknown", "reason": "inspection_failed", "message": "permission denied"})
        summary = assess_status_summary(observations)
        self.assertEqual(summary["runtime"], {
            "state": "unknown", "reason": "inspection_incomplete"})
        self.assertEqual(project_status_line(summary)["row"]["value"], "unknown")
        self.assertEqual(status_terminal(raw), "tap           ? unknown · inspection incomplete")

    def test_foreign_listener_and_broken_service_are_distinct_claims(self):
        conflict = snapshot(service_loaded=False, pid=None, port_owned=False, port_open=True)
        self.assertEqual(assess_status_summary(normalize_status_observations(conflict))["runtime"], {
            "state": "port_conflict", "reason": "listener_not_owned_by_profile", "port": 18999})
        self.assertEqual(status_terminal(conflict), "tap           ✗ PORT STOLEN")
        broken = snapshot(port_owned=False, port_open=False)
        self.assertEqual(status_terminal(broken), "tap           ✗ broken · service has no listener")

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
                self.assertEqual(human.getvalue(), "tap           ● up · PID 123\n")


if __name__ == "__main__":
    unittest.main()
