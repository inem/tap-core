#!/usr/bin/env python3
"""Read-only check for changes in the two legacy components being transferred."""
import argparse
import hashlib
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    args = parser.parse_args()
    docs = Path(__file__).resolve().parents[1] / "docs"
    capture = json.loads((docs / "legacy-characterization-2026-09-07.json").read_text())
    cli = json.loads((docs / "legacy-cli-characterization-2026-09-07.json").read_text())
    expected = {"tap": cli["source_sha256"], "capture.py": capture["source_sha256"]["capture.py"]}
    results = {}
    for name, digest in expected.items():
        path = args.source / name
        actual = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        results[name] = {"expected": digest, "actual": actual, "unchanged": actual == digest}
    print(json.dumps(results, indent=2))
    return 0 if all(item["unchanged"] for item in results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
