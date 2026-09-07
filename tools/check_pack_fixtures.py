#!/usr/bin/env python3
"""Run repository-owned pack fixtures only; this is not the installed pack host."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tap_core.packs import check_activation, fixture_context, load_manifest

FIXTURES = ROOT / "fixtures" / "packs"
# Test policy is independently supplied, never derived from the manifest requests.
ORIGINS = ["https://fixture.example"]
CAPABILITIES = ["capture.read", "response.mutate", "page.inject", "bridge.handle"]


def prepare(pack, profile, overrides=None):
    manifest = load_manifest(pack)
    check_activation(manifest, ORIGINS, CAPABILITIES, {})
    context = fixture_context(manifest, profile, overrides)
    for key in ("state_dir", "output_dir", "log_dir"):
        Path(context[key]).mkdir(parents=True, exist_ok=True)
    return manifest, context


def python_entry(pack, manifest, role, context, text):
    env = dict(os.environ, TAP_PACK_CONTEXT=json.dumps(context))
    return subprocess.run([sys.executable, str(pack / manifest["entrypoints"][role]["file"])],
                          input=text, text=True, capture_output=True, env=env,
                          cwd=context["state_dir"], timeout=10)


class ResponseFixture:
    def __init__(self, content_type="text/html", streamed=False):
        self.headers = {"content-type": content_type, "etag": "before", "content-length": "28"}
        self.stream = streamed
        self.body = "<html><body>ok</body></html>"

    def get_text(self):
        if self.stream:
            raise AssertionError("streamed response must not be read")
        return self.body

    def set_text(self, value):
        self.body = value


def check_mutator(pack, manifest):
    path = pack / manifest["entrypoints"]["mutator"]["file"]
    spec = importlib.util.spec_from_file_location("fixture_mutator", path)
    module = importlib.util.module_from_spec(spec)
    previous = sys.dont_write_bytecode
    try:
        sys.dont_write_bytecode = True
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    for content_type in ("text/html", "  TeXt/HTML ; charset=utf-8"):
        flow = SimpleNamespace(request=SimpleNamespace(pretty_url="https://fixture.example/page"),
                               response=ResponseFixture(content_type))
        module.response(flow)
        assert 'src="/__tap/fixture/page.js"' in flow.response.body
        assert "etag" not in flow.response.headers and "content-length" not in flow.response.headers
        once = flow.response.body
        module.response(flow)
        assert flow.response.body == once
    for url, content_type, streamed in [
        ("https://unrelated.example/page", "text/html", False),
        ("https://fixture.example:8443/page", "text/html", False),
        ("https://fixture.example/data", "application/json", False),
        ("https://fixture.example/data", 'application/json; profile="text/html"', False),
        ("https://fixture.example/data", "text/html-not-really", False),
        ("https://fixture.example/events", "text/html", True),
    ]:
        flow = SimpleNamespace(request=SimpleNamespace(pretty_url=url),
                               response=ResponseFixture(content_type, streamed))
        before = (flow.response.body, dict(flow.response.headers))
        module.response(flow)
        assert (flow.response.body, flow.response.headers) == before


def run_python():
    with tempfile.TemporaryDirectory(prefix="tap-pack-fixtures-") as directory:
        profile = Path(directory)
        reader = FIXTURES / "reader"
        page = FIXTURES / "page-bridge"
        # Validate both packs and access before importing or executing either.
        reader_manifest, reader_context = prepare(reader, profile, {"prefix": "checked"})
        page_manifest, page_context = prepare(page, profile)
        result = python_entry(reader, reader_manifest, "reader", reader_context,
                              (FIXTURES / "records.jsonl").read_text())
        assert result.returncode == 0, result.stderr
        assert [json.loads(line) for line in result.stdout.splitlines()] == [
            {"label": "checked", "body": {"fixture": "tap-core"}}]
        assert (Path(reader_context["output_dir"]) / "observations.jsonl").read_text() == result.stdout
        assert [json.loads(line)["event"] for line in result.stderr.splitlines()] == ["start", "stop"]
        reply = python_entry(page, page_manifest, "handler", page_context,
                             json.dumps({"op": "echo", "text": "hello"}) + "\n")
        assert reply.returncode == 0, reply.stderr
        assert json.loads(reply.stdout) == {"ok": True, "text": "local:hello"}
        assert [json.loads(line)["event"] for line in reply.stderr.splitlines()] == ["start", "stop"]
        for pack, manifest, role, context in [
            (reader, reader_manifest, "reader", reader_context),
            (page, page_manifest, "handler", page_context),
        ]:
            failure = python_entry(pack, manifest, role, context, "not json\n")
            assert failure.returncode != 0 and not failure.stdout
            assert json.loads(failure.stderr.splitlines()[-1])["event"] == "error"
        check_mutator(page, page_manifest)
        return {"pack_api": 1, "evidence": "synthetic fixtures only",
                "reader": "passed", "handler": "passed", "mutator": "passed",
                "start_stop_error": "passed", "live_ws": "not tested", "installed_host": "not implemented"}


def run_page(bun):
    with tempfile.TemporaryDirectory(prefix="tap-page-fixture-") as directory:
        page = FIXTURES / "page-bridge"
        page_manifest, page_context = prepare(page, Path(directory))
        env = dict(os.environ, TAP_PACK_CONTEXT=json.dumps(page_context))
        result = subprocess.run([str(bun), str(FIXTURES / "check_page.mjs"),
                                 str(page / page_manifest["entrypoints"]["page"]["file"]),
                                 sys.executable, str(page / page_manifest["entrypoints"]["handler"]["file"])],
                                capture_output=True, text=True, env=env,
                                cwd=page_context["state_dir"], timeout=10)
        assert result.returncode == 0, result.stderr
        page_result = json.loads(result.stdout)
        assert page_result == {"started": "local:hello", "stopped": True, "bad_reply_rejected": True}
        return {"page_to_handler_shape": "passed"}


def run(bun=None):
    result = run_python()
    result.update(run_page(bun) if bun else {"page_to_handler_shape": "not tested: supply --bun"})
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bun", type=Path, help="explicit Bun executable; omit to run Python fixtures only")
    args = parser.parse_args()
    print(json.dumps(run(args.bun.resolve() if args.bun else None), indent=2))
