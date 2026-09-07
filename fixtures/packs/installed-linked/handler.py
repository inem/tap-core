"""Hub-facing pack handler: stdin Hub envelope → JSON {ok, value|error}.

Hub.invoke sends one line: {version, request_id, args}. Result must include
boolean ok and either value or typed error — not the fixture page-bridge echo shape.
"""
import json
import os
import sys


def main():
    context = json.loads(os.environ["TAP_PACK_CONTEXT"])
    request = json.loads(sys.stdin.readline())
    if type(request) is not dict or request.get("version") != 1 or type(request.get("args")) is not dict:
        print(json.dumps({"ok": False, "error": {
            "code": "bad_request", "message": "expected Hub envelope {version:1, request_id, args}"}}))
        return
    args = request["args"]
    text = args.get("text")
    if type(text) is not str:
        print(json.dumps({"ok": False, "error": {
            "code": "bad_request", "message": "args.text must be a string"}}))
        return
    prefix = context.get("config", {}).get("reply-prefix", "")
    print(json.dumps({"ok": True, "value": {"text": prefix + text}}))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"ok": False, "error": {
            "code": "handler_failed", "message": str(error)}}))
        sys.exit(1)
