#!/usr/bin/env python3
"""Synthetic command provider. The host never imports this file."""

import json
import os
from pathlib import Path
import sys
import time


marker = os.environ.get("TAP_FIXTURE_EXECUTION_MARKER")
if marker:
    Path(marker).touch()

context = json.loads(os.environ["TAP_COMMAND_CONTEXT"])
path = tuple(context["path"])

if path == ("fixture", "echo"):
    print(json.dumps({
        "argv": sys.argv[1:],
        "provider": context["provider"],
        "prefix": context["config"]["prefix"],
        "profile": context["profile"],
    }, ensure_ascii=False))
    raise SystemExit(0)

if path == ("fixture", "fail"):
    print("fixture provider failure", file=sys.stderr)
    raise SystemExit(int(sys.argv[1]))

if path == ("fixture", "wait"):
    release = Path(sys.argv[1])
    if len(sys.argv) > 2:
        Path(sys.argv[2]).write_text(str(os.getpid()))
    print("ready", flush=True)
    wait_timeout = float(os.environ.get("TAP_FIXTURE_WAIT_TIMEOUT", "15"))
    deadline = time.monotonic() + wait_timeout
    while not release.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    if not release.exists():
        print("fixture wait timed out", file=sys.stderr)
        raise SystemExit(1)
    raise SystemExit(0)

print("unknown fixture command path", file=sys.stderr)
raise SystemExit(2)
