"""Local echo fixture. Transport/authentication are deliberately host concerns."""
import json
import os
import sys


def main():
    context = json.loads(os.environ["TAP_PACK_CONTEXT"])
    print(json.dumps({"event": "start"}), file=sys.stderr)
    for line in sys.stdin:
        request = json.loads(line)
        if set(request) != {"op", "text"} or request["op"] != "echo" or type(request["text"]) is not str:
            raise ValueError("expected an echo request with string text")
        print(json.dumps({"ok": True, "text": context["config"]["reply-prefix"] + request["text"]}), flush=True)
    print(json.dumps({"event": "stop"}), file=sys.stderr)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"event": "error", "message": str(error)}), file=sys.stderr)
        sys.exit(1)
