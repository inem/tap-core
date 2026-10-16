"""HTTP capture record v1 and read-only compatibility with unversioned records."""
import base64
import json
import math
import uuid

RECORD_VERSION = 1
# A reader-side allocation limit, not a total process-memory or disk quota.
MAX_RECORD_BYTES = 32 * 1024 * 1024


class RecordError(ValueError):
    pass


def validate_record(record, allow_legacy=True):
    if not isinstance(record, dict):
        raise RecordError("Capture record must be an object")
    version = record.get("record_version", 0)
    if type(version) is not int or version not in (0, RECORD_VERSION):
        raise RecordError("Unsupported capture record version")
    if version == 0 and not allow_legacy:
        raise RecordError("Unversioned capture record is not allowed")
    if not isinstance(record.get("url"), str) or not record["url"]:
        raise RecordError("Invalid capture url")
    # mitmproxy preserves non-standard three-digit statuses (for example a
    # site's telemetry response with 999). Capture must remain readable even
    # when the remote peer does not use an IANA-assigned HTTP status class.
    if type(record.get("status")) is not int or not 100 <= record["status"] <= 999:
        raise RecordError("Invalid HTTP response status")
    for name in ("body_kept", "streamed", "req_body_kept"):
        if name in record and type(record[name]) is not bool:
            raise RecordError("Invalid capture flag: " + name)
    for name in ("body", "req_body"):
        if name in record and record[name] is not None and not isinstance(record[name], str):
            raise RecordError("Invalid capture text: " + name)
    if version == 0:
        # Old examples omit fields; do not invent IDs, body reasons or coverage.
        return record
    try:
        identifier = record["record_id"]
        if not isinstance(identifier, str) or str(uuid.UUID(identifier)) != identifier:
            raise ValueError()
    except (KeyError, ValueError, AttributeError):
        raise RecordError("Invalid capture record_id") from None
    for name in ("method", "ctype", "ua"):
        if not isinstance(record.get(name), str):
            raise RecordError("Invalid capture text: " + name)
    timestamp = record.get("ts")
    if (type(timestamp) not in (int, float) or timestamp < 0
            or (type(timestamp) is float and not math.isfinite(timestamp))):
        raise RecordError("Invalid capture timestamp")
    if type(record.get("size")) is not int or record["size"] < 0:
        raise RecordError("Invalid capture size")
    if type(record.get("streamed")) is not bool:
        raise RecordError("Missing capture streamed flag")
    for flag, body, reason, allowed in (
        ("body_kept", "body", "body_reason",
         {"retained", "media_type", "streamed", "unavailable", "oversize", "unbounded", "oversize_decoded", "undecodable"}),
        ("req_body_kept", "req_body", "req_body_reason",
         {"retained", "response_not_retained", "streamed", "unavailable", "oversize_decoded", "undecodable"}),
    ):
        value = record.get(reason)
        if type(record.get(flag)) is not bool or not isinstance(value, str) or value not in allowed:
            raise RecordError("Invalid body disposition: " + reason)
        if record[flag]:
            if value != "retained" or not isinstance(record.get(body), str):
                raise RecordError("Retained body must contain text: " + body)
        elif value == "retained" or body in record:
            raise RecordError("Omitted body must not contain text: " + body)
    if record["body_reason"] == "streamed" and not record["streamed"]:
        raise RecordError("Streamed body reason requires streaming")
    if record["streamed"] and record["body_reason"] == "unavailable":
        raise RecordError("Streamed body must report streaming or media type policy")
    if record["streamed"] and record["body_kept"]:
        raise RecordError("Streamed response cannot retain its body")
    if record["body_kept"] and record["req_body_reason"] == "response_not_retained":
        raise RecordError("Inconsistent request body reason")
    if "body_encoding" in record:
        if record["body_encoding"] not in ("base64",):
            raise RecordError("Invalid body_encoding")
        if not record["body_kept"]:
            raise RecordError("body_encoding requires a retained body")
    return record


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RecordError("Duplicate capture field: " + key)
        result[key] = value
    return result


def _constant(value):
    raise RecordError("Non-finite JSON number: " + value)


def decode_record(line, allow_legacy=True):
    try:
        if isinstance(line, bytes):
            line = line.decode("utf-8")
        record = json.loads(line, object_pairs_hook=_object,
                            parse_constant=_constant)
    except (ValueError, UnicodeError) as error:
        raise RecordError("Invalid capture JSON: " + str(error)) from error
    return validate_record(record, allow_legacy=allow_legacy)


# Stream parts in data/wire.jsonl (#250). A separate journal with its own
# schema: stream.jsonl and the v1 record above stay unchanged. A flow's parts
# share `flow_id` (= the v1 record_id of the same flow) and are numbered from 0;
# concatenating `data` of all parts in order gives the first `bytes_kept` bytes
# the client received (content-encoding not decoded). A missing part number is
# a loss; the next delivered part counts it in `parts_lost_before`.
WIRE_SCHEMA = "tap.capture.wire/v1"
WIRE_OUTCOMES = ("complete", "truncated", "error", "shutdown", "refused")


def _uuid(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def _time(value):
    return type(value) in (int, float) and value >= 0 and math.isfinite(value)


def _count(value):
    return type(value) is int and value >= 0


def validate_wire_part(record):
    if not isinstance(record, dict) or record.get("schema") != WIRE_SCHEMA:
        raise RecordError("Unsupported wire part schema")
    for name in ("record_id", "flow_id"):
        if not _uuid(record.get(name)):
            raise RecordError("Invalid wire part " + name)
    if not _count(record.get("part")) or type(record.get("final")) is not bool:
        raise RecordError("Invalid wire part number or final flag")
    if record.get("transport") not in ("http", "websocket"):
        raise RecordError("Invalid wire transport")
    if not isinstance(record.get("url"), str) or not record["url"]:
        raise RecordError("Invalid wire url")
    for name in ("method", "ctype", "content_encoding", "ua"):
        if not isinstance(record.get(name), str):
            raise RecordError("Invalid wire text: " + name)
    if type(record.get("status")) is not int or not 100 <= record["status"] <= 999:
        raise RecordError("Invalid HTTP response status")
    if not _time(record.get("t_headers")) or not (
            record.get("t_request") is None or _time(record.get("t_request"))):
        raise RecordError("Invalid wire flow timestamps")
    chunks = record.get("chunks")
    if not isinstance(chunks, list):
        raise RecordError("Invalid wire chunks")
    websocket = record["transport"] == "websocket"
    for chunk in chunks:
        if (not isinstance(chunk, list) or len(chunk) != (4 if websocket else 2)
                or not _count(chunk[0]) or not _count(chunk[1])
                or (websocket and (chunk[2] not in ("c", "s") or not _count(chunk[3])))):
            raise RecordError("Invalid wire chunk entry")
    if chunks:
        if not (_time(record.get("t_first")) and _time(record.get("t_last"))
                and record["t_first"] <= record["t_last"]):
            raise RecordError("Invalid wire part timestamps")
    elif record.get("t_first") is not None or record.get("t_last") is not None:
        raise RecordError("Wire part without chunks has no timestamps")
    data = record.get("data")
    try:
        raw = base64.b64decode(data, validate=True) if isinstance(data, str) else None
    except ValueError:
        raw = None
    if raw is None:
        raise RecordError("Wire part data must be base64 text")
    if len(raw) > sum(chunk[1] for chunk in chunks):
        raise RecordError("Wire part keeps more bytes than its chunks carried")
    for name in ("bytes_seen", "bytes_kept", "parts_lost_before"):
        if not _count(record.get(name)):
            raise RecordError("Invalid wire counter: " + name)
    if record["bytes_kept"] > record["bytes_seen"]:
        raise RecordError("Wire part keeps more bytes than it saw")
    outcome = record.get("outcome")
    if record["final"] != (outcome is not None) or (
            outcome is not None and outcome not in WIRE_OUTCOMES):
        raise RecordError("Invalid wire outcome")
    if "error" in record and not isinstance(record["error"], str):
        raise RecordError("Invalid wire error text")
    if "close_code" in record and record["close_code"] is not None and not _count(
            record["close_code"]):
        raise RecordError("Invalid WebSocket close code")
    return record


def decode_wire_part(line):
    try:
        if isinstance(line, bytes):
            line = line.decode("utf-8")
        record = json.loads(line, object_pairs_hook=_object, parse_constant=_constant)
    except (ValueError, UnicodeError) as error:
        raise RecordError("Invalid wire JSON: " + str(error)) from error
    return validate_wire_part(record)
