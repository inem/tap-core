#!/usr/bin/env python3
"""Integration-only synthetic writer recovery -> reader pack check.

Requires the combined capture-recovery and pack-contract branches. Does not
start a service, use a network endpoint or implement installed-pack delivery.
"""
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tap_core.runtime import Profile
from tap_core.capture import Capture, Writer
from tools.check_pack_fixtures import FIXTURES, prepare, python_entry


def main():
    with tempfile.TemporaryDirectory(prefix="tap-combined-pipeline-") as directory:
        profile = Profile(Path(directory), "/fixture/mitmdump", 18999, "explicit", "http://example.test/", [])
        profile.save()
        stream = profile.root / "data/stream.jsonl"
        historical = {"url": "https://fixture.example/saved", "status": 200,
                      "body": json.dumps({"fixture": "historical"})}
        preserved = json.dumps(historical).encode() + b"\n"
        stream.write_bytes(preserved + b'{"torn":')
        writer = Writer(profile.root / "data", profile.root / "state")
        body = json.dumps({"fixture": "new"})
        request = SimpleNamespace(method="GET", url="https://fixture.example/new", headers={},
                                  stream=False, get_text=lambda **kwargs: "")
        response = SimpleNamespace(status_code=200, headers={"content-type": "Application/JSON",
                                   "content-length": str(len(body))}, stream=False, get_text=lambda **kwargs: body)
        capture = Capture(writer)
        flow = SimpleNamespace(request=request, response=response)
        try:
            capture.responseheaders(flow)
            capture.response(flow)
        finally:
            writer.close()
        assert not writer.thread.is_alive()
        assert stream.read_bytes().startswith(preserved)
        assert len(stream.read_text().splitlines()) == 2
        health = json.loads((profile.root / "state/capture.json").read_text())
        assert health["dropped"] == 1 and health["write_errors"] >= 1
        pack = FIXTURES / "reader"
        manifest, context = prepare(pack, profile.root, {"prefix": "pipeline"})
        result = python_entry(pack, manifest, "reader", context, stream.read_text())
        assert result.returncode == 0, result.stderr
        expected = [{"label": "pipeline", "body": {"fixture": value}} for value in ("historical", "new")]
        assert [json.loads(line) for line in result.stdout.splitlines()] == expected
        assert (Path(context["output_dir"]) / "observations.jsonl").read_text() == result.stdout
        print(json.dumps({"scope": "combined synthetic capture/storage/reader fixture; not installed-pack or live WS integration",
                          "complete_prefix_preserved": True, "torn_tail_reported": True,
                          "reader_outputs": expected}, indent=2))


if __name__ == "__main__":
    main()
