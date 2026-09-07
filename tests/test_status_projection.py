import contextlib
from dataclasses import replace
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tap_core.cli import main
from tap_core.runtime import Profile
from tap_core.status_projection import (
    STATUS_RESULT,
    SurfaceToken,
    interpret_status_result,
    project_status,
    public_status_result,
    serialize_terminal,
    status_terminal,
    validate_projection,
    validate_surface,
)


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


def semantic_sources(surface):
    return {
        source
        for line in surface.lines
        for token in line.tokens
        if token.kind == "semantic"
        for source in token.sources
    }


class StatusProjectionTests(unittest.TestCase):
    def test_complete_validated_path_matches_two_legacy_rows(self):
        trace = project_status(snapshot())
        validate_projection(trace)
        self.assertEqual(
            serialize_terminal(trace.surface),
            "tap           ● up · PID 123\n"
            "browser/apps  ○ direct   (tap on)",
        )

    def test_public_result_is_versioned_and_preserves_unknown_evidence(self):
        result = public_status_result(snapshot(
            pid=None, inspection_errors={"pid": "permission denied"}))
        self.assertEqual(result["schema"], STATUS_RESULT)
        self.assertEqual(result["runtime"]["pid"], {
            "knowledge": "unknown",
            "reason": "inspection_failed",
            "message": "permission denied",
        })
        self.assertEqual(json.loads(json.dumps(result)), result)
        model = interpret_status_result(result)
        self.assertEqual(model.claims[0].value, "unknown")
        self.assertEqual(
            status_terminal(snapshot(
                pid=None, inspection_errors={"pid": "permission denied"})).splitlines()[0],
            "tap           ? unknown · inspection incomplete",
        )

    def test_claims_retain_all_observation_warrants(self):
        trace = project_status(snapshot())
        runtime, routing = trace.model.claims
        self.assertEqual(set(runtime.warrants), {
            "runtime.service_loaded", "runtime.pid", "runtime.port_owned", "runtime.port_open"})
        self.assertEqual(set(routing.warrants), {
            "routing.routing", "routing.network_recovery_pending",
            "routing.system_proxy_verified"})

    def test_every_meaningful_terminal_mark_has_a_report_source(self):
        trace = project_status(snapshot())
        tokens = [token for line in trace.surface.lines for token in line.tokens]
        dot = next(token for token in tokens if token.text == "·")
        circle = next(token for token in tokens if token.text == "○")
        opening = next(token for token in tokens if token.text == "(")
        closing = next(token for token in tokens if token.text == ")")
        self.assertEqual(dot.sources, ("runtime.pid",))
        self.assertEqual(circle.sources, ("routing.indicator",))
        self.assertEqual(opening.sources, ("routing.attachment",))
        self.assertEqual(closing.sources, ("routing.attachment",))
        self.assertTrue(all(token.sources for token in tokens if token.kind == "semantic"))
        self.assertTrue(all(not token.sources and token.layout_role
                            for token in tokens if token.kind == "layout"))

    def test_removing_meaning_without_an_omission_breaks_the_invariant(self):
        trace = project_status(snapshot())
        lines = list(trace.surface.lines)
        tokens = list(lines[0].tokens)
        del tokens[4]
        lines[0] = replace(lines[0], tokens=tuple(tokens))
        broken_surface = replace(trace.surface, lines=tuple(lines))
        with self.assertRaisesRegex(ValueError, "silently dropped"):
            validate_surface(trace.report, broken_surface)

    def test_invented_surface_source_breaks_the_invariant(self):
        trace = project_status(snapshot())
        lines = list(trace.surface.lines)
        lines[0] = replace(lines[0], tokens=lines[0].tokens + (SurfaceToken(
            text="invented", kind="semantic", sources=("meaning.not-in-report",)),))
        broken_surface = replace(trace.surface, lines=tuple(lines))
        with self.assertRaisesRegex(ValueError, "no valid source"):
            validate_surface(trace.report, broken_surface)

    def test_line_structure_and_layout_roles_are_part_of_the_trace(self):
        trace = project_status(snapshot())
        missing_line = replace(trace.surface, lines=trace.surface.lines[:1])
        with self.assertRaisesRegex(ValueError, "line structure"):
            validate_surface(trace.report, missing_line)

        lines = list(trace.surface.lines)
        tokens = list(lines[0].tokens)
        tokens[1] = replace(tokens[1], layout_role=None)
        lines[0] = replace(lines[0], tokens=tuple(tokens))
        unowned_spacing = replace(trace.surface, lines=tuple(lines))
        with self.assertRaisesRegex(ValueError, "declared layout role"):
            validate_surface(trace.report, unowned_spacing)

    def test_narrow_layout_makes_optional_omissions_explicit(self):
        trace = project_status(snapshot(), width=24)
        self.assertEqual(
            serialize_terminal(trace.surface),
            "tap           ● up\n"
            "browser/apps  ○ direct",
        )
        self.assertEqual({omission.target for omission in trace.surface.omissions},
                         {"runtime.pid", "routing.attachment"})
        self.assertTrue(all(omission.reason == "terminal-width:24"
                            for omission in trace.surface.omissions))

    def test_theme_changes_notation_without_changing_semantic_coverage(self):
        unicode_trace = project_status(snapshot(), theme="unicode")
        ascii_trace = project_status(snapshot(), theme="ascii")
        self.assertEqual(semantic_sources(unicode_trace.surface),
                         semantic_sources(ascii_trace.surface))
        self.assertEqual(
            serialize_terminal(ascii_trace.surface),
            "tap           + up - PID 123\n"
            "browser/apps  o direct   [tap on]",
        )

    def test_distinct_routing_claims_survive_projection(self):
        cases = [
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
        for fields, claim, line in cases:
            with self.subTest(fields=fields):
                trace = project_status(snapshot(**fields))
                self.assertEqual(trace.model.claims[1].value, claim)
                self.assertEqual(serialize_terminal(trace.surface).splitlines()[1], line)

    def test_invariant_holds_across_state_theme_and_width_axes(self):
        states = [
            {},
            {"service_loaded": False, "pid": None, "port_owned": False,
             "port_open": False},
            {"port_owned": False, "port_open": False},
            {"routing": "system", "network_recovery_pending": True,
             "system_proxy_verified": True},
            {"routing": "explicit", "network_recovery_pending": False,
             "system_proxy_verified": "not_used"},
            {"system_proxy_verified": None,
             "inspection_errors": {"system_proxy_verified": "denied"}},
        ]
        for state in states:
            for theme in ("unicode", "ascii"):
                for width in (24, 80):
                    with self.subTest(state=state, theme=theme, width=width):
                        trace = project_status(snapshot(**state), width=width, theme=theme)
                        validate_projection(trace)

    def test_cli_preserves_raw_json_and_exposes_both_experimental_outputs(self):
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

                semantic = io.StringIO()
                with contextlib.redirect_stdout(semantic):
                    self.assertEqual(main([
                        "--profile", directory, "status", "--output", "semantic-json"]), 0)
                self.assertEqual(json.loads(semantic.getvalue()), public_status_result(raw))

                terminal = io.StringIO()
                with contextlib.redirect_stdout(terminal):
                    self.assertEqual(main([
                        "--profile", directory, "status", "--output", "terminal",
                        "--color", "never", "--width", "80"]), 0)
                self.assertEqual(
                    terminal.getvalue(),
                    "tap           ● up · PID 123\n"
                    "browser/apps  ○ direct   (tap on)\n",
                )


if __name__ == "__main__":
    unittest.main()
