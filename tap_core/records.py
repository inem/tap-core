"""HTTP capture record v1 and read-only compatibility with unversioned records."""
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
    if type(record.get("status")) is not int or not 100 <= record["status"] <= 599:
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
        ("body_kept", "body", "body_reason", {"retained", "media_type", "streamed", "unavailable"}),
        ("req_body_kept", "req_body", "req_body_reason",
         {"retained", "response_not_retained", "streamed", "unavailable"}),
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
    if not record["body_kept"] and record["req_body_reason"] != "response_not_retained":
        raise RecordError("Request capture requires a retained response")
    if record["body_kept"] and record["req_body_reason"] == "response_not_retained":
        raise RecordError("Inconsistent request body reason")
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
