"""Synthetic reader; current record examples are inputs, not a capture schema."""
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit


def main():
    context = json.loads(os.environ["TAP_PACK_CONTEXT"])
    print(json.dumps({"event": "start"}), file=sys.stderr)
    for line in sys.stdin:
        record = json.loads(line)
        url = urlsplit(record.get("url", ""))
        if (url.scheme, url.netloc) != ("https", "fixture.example") or record.get("status") != 200:
            continue
        if not isinstance(record.get("body"), str):
            continue
        result = {"label": context["config"]["prefix"], "body": json.loads(record["body"])}
        # Files are confined by convention, not a sandbox or a reader checkpoint API.
        with (Path(context["output_dir"]) / "observations.jsonl").open("a", encoding="utf-8") as target:
            target.write(json.dumps(result) + "\n")
        print(json.dumps(result), flush=True)
    print(json.dumps({"event": "stop"}), file=sys.stderr)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"event": "error", "message": str(error)}), file=sys.stderr)
        sys.exit(1)
