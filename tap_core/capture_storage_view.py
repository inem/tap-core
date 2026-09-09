"""Bounded Core capture-storage observation for ``tap where``."""
import json
from pathlib import Path

from .capture import capture_limits
from .filesystem_sensor import inspect_sized_path
from .material_view import observe
from .projection import Atom, Derivation, ProjectionError, evaluate_rules


ADAPTER = Path(__file__).with_name("data") / "where-capture-storage.json"
CARRIER_ADAPTER = Path(__file__).with_name("data") / "where-capture-storage-carrier.json"


def _read_object(path, kind):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ProjectionError(f"cannot load {kind}: {error}") from error
    if not isinstance(value, dict):
        raise ProjectionError(f"invalid {kind}")
    return value


def _validate_source(source):
    try:
        observe(source, {})
    except ProjectionError as error:
        raise ProjectionError("invalid capture storage source") from error


def validate_carrier_adapter(adapter):
    required = {"schema", "id", "receipt_schema", "errors_path", "stream", "policy_source"}
    stream = adapter.get("stream") if isinstance(adapter, dict) else None
    if (not isinstance(adapter, dict) or set(adapter) != required
            or adapter.get("schema") != "tap.where-capture-storage-carrier/v1"
            or adapter.get("receipt_schema") != "tap.capture-storage-receipts/v1"
            or not isinstance(adapter.get("id"), str) or not adapter["id"]
            or not isinstance(stream, dict)
            or not isinstance(adapter.get("errors_path"), list)
            or not adapter["errors_path"]
            or not all(isinstance(part, str) and part for part in adapter["errors_path"])
            or set(stream) != {"id", "base_address_source", "relative_path"}
            or stream.get("id") != "capture_stream"
            or stream.get("relative_path") != "stream.jsonl"):
        raise ProjectionError("invalid capture storage carrier adapter")
    _validate_source(stream["base_address_source"])
    _validate_source(adapter["policy_source"])


def validate_adapter(adapter):
    required = {"schema", "id", "receipt_schema", "output_schema", "semantic_rules"}
    if (not isinstance(adapter, dict) or set(adapter) != required
            or adapter.get("schema") != "tap.capture-storage-observation-adapter/v1"
            or adapter.get("receipt_schema") != "tap.capture-storage-receipts/v1"
            or adapter.get("output_schema") != "tap.capture-storage-observation/v1"
            or not isinstance(adapter.get("id"), str) or not adapter["id"]
            or not isinstance(adapter.get("semantic_rules"), list)):
        raise ProjectionError("invalid capture storage observation adapter")
    identifiers = [rule.get("id") for rule in adapter["semantic_rules"]
                   if isinstance(rule, dict)]
    if (len(identifiers) != len(adapter["semantic_rules"])
            or any(not isinstance(value, str) or not value for value in identifiers)
            or len(set(identifiers)) != len(identifiers)):
        raise ProjectionError("invalid capture storage rule")


def load_adapter(path=ADAPTER):
    adapter = _read_object(path, "capture storage adapter")
    validate_adapter(adapter)
    return adapter


def load_carrier_adapter(path=CARRIER_ADAPTER):
    adapter = _read_object(path, "capture storage carrier adapter")
    validate_carrier_adapter(adapter)
    return adapter


def capture_receipts(snapshot, adapter=None, sensor=inspect_sized_path):
    """Resolve one declared child path and inspect it with one bounded lstat."""
    adapter = load_carrier_adapter() if adapter is None else adapter
    validate_carrier_adapter(adapter)
    errors = snapshot
    for part in adapter["errors_path"]:
        if not isinstance(errors, dict) or part not in errors:
            errors = {}
            break
        errors = errors[part]
    if not isinstance(errors, dict):
        raise ProjectionError("capture storage carrier errors must be an object")
    base = observe(adapter["stream"]["base_address_source"], snapshot, errors)
    if base["knowledge"] == "known":
        if not isinstance(base["value"], str) or not base["value"]:
            raise ProjectionError("invalid capture stream base address")
        address = str(Path(base["value"]) / adapter["stream"]["relative_path"])
        stream = sensor(address)
    else:
        address = None
        outcome = ("source_failed" if base.get("reason") == "inspection_failed"
                   else "not_observed")
        stream = {"outcome": outcome, "kind": None, "size_bytes": None,
                  "errno": None, "message": base["message"]}

    supplied = observe(adapter["policy_source"], snapshot, errors)
    if supplied["knowledge"] == "known":
        try:
            limits = capture_limits(supplied["value"])
        except (TypeError, ValueError) as error:
            policy = {"outcome": "failed", "source": None, "segment_bytes": None,
                      "keep_rolls": None, "ceiling_bytes": None,
                      "message": str(error)}
        else:
            policy = {
                "outcome": "effective",
                "source": "defaults" if supplied["value"] is None else "profile",
                "segment_bytes": limits["segment_bytes"],
                "keep_rolls": limits["keep_rolls"],
                "ceiling_bytes": limits["segment_bytes"] * (limits["keep_rolls"] + 1),
                "message": "",
            }
    else:
        outcome = ("failed" if supplied.get("reason") == "inspection_failed"
                   else "not_observed")
        policy = {"outcome": outcome, "source": None, "segment_bytes": None,
                  "keep_rolls": None, "ceiling_bytes": None,
                  "message": supplied["message"]}
    return {"schema": adapter["receipt_schema"], "stream_id": adapter["stream"]["id"],
            "stream_address": address, "stream": stream, "policy": policy}


def _validate_stream_receipt(receipt):
    if (not isinstance(receipt, dict)
            or set(receipt) != {"outcome", "kind", "size_bytes", "errno", "message"}
            or receipt.get("outcome") not in
            ("present", "absent", "failed", "source_failed", "not_observed")
            or not isinstance(receipt.get("message"), str)
            or (receipt.get("errno") is not None and type(receipt["errno"]) is not int)):
        raise ProjectionError("invalid capture stream receipt")
    if receipt["outcome"] == "present":
        if receipt["kind"] not in ("regular", "symlink", "other"):
            raise ProjectionError("invalid present capture stream receipt")
        expected_size = receipt["kind"] == "regular"
        if expected_size != (type(receipt["size_bytes"]) is int and receipt["size_bytes"] >= 0):
            raise ProjectionError("invalid capture stream receipt size")
    elif receipt["kind"] is not None or receipt["size_bytes"] is not None:
        raise ProjectionError("invalid non-present capture stream receipt")


def _validate_policy_receipt(receipt):
    keys = {"outcome", "source", "segment_bytes", "keep_rolls", "ceiling_bytes", "message"}
    if not isinstance(receipt, dict) or set(receipt) != keys or not isinstance(receipt.get("message"), str):
        raise ProjectionError("invalid capture policy receipt")
    if receipt["outcome"] == "effective":
        if (receipt["source"] not in ("defaults", "profile")
                or type(receipt["segment_bytes"]) is not int or receipt["segment_bytes"] < 1
                or type(receipt["keep_rolls"]) is not int or receipt["keep_rolls"] < 0
                or receipt["ceiling_bytes"] != receipt["segment_bytes"] * (receipt["keep_rolls"] + 1)):
            raise ProjectionError("invalid effective capture policy receipt")
    elif (receipt["outcome"] not in ("failed", "not_observed")
          or any(receipt[name] is not None for name in
                 ("source", "segment_bytes", "keep_rolls", "ceiling_bytes"))):
        raise ProjectionError("invalid unknown capture policy receipt")


def public_result(carrier, adapter=None):
    """Translate physical receipts into a versioned storage observation by rules."""
    adapter = load_adapter() if adapter is None else adapter
    validate_adapter(adapter)
    if (not isinstance(carrier, dict)
            or set(carrier) != {"schema", "stream_id", "stream_address", "stream", "policy"}
            or carrier.get("schema") != adapter["receipt_schema"]
            or carrier.get("stream_id") != "capture_stream"
            or (carrier.get("stream_address") is not None
                and (not isinstance(carrier["stream_address"], str) or not carrier["stream_address"]))):
        raise ProjectionError("invalid capture storage receipt carrier")
    _validate_stream_receipt(carrier["stream"])
    _validate_policy_receipt(carrier["policy"])
    stream = carrier["stream"]
    policy = carrier["policy"]
    facts = {
        Atom.from_value(["capture-stream-receipt", carrier["stream_id"],
                         carrier["stream_address"], stream["outcome"], stream["kind"],
                         stream["size_bytes"], stream["errno"], stream["message"]]):
            Derivation("supplied-capture-storage-receipt", tuple()),
        Atom.from_value(["capture-policy-receipt", policy["outcome"], policy["source"],
                         policy["segment_bytes"], policy["keep_rolls"],
                         policy["ceiling_bytes"], policy["message"]]):
            Derivation("supplied-capture-storage-receipt", tuple()),
    }
    projected = evaluate_rules(facts, adapter["semantic_rules"])
    stream_atoms = [atom for atom in projected if atom.relation == "capture-stream-observation"]
    policy_atoms = [atom for atom in projected if atom.relation == "capture-policy-observation"]
    if len(stream_atoms) != 1 or len(policy_atoms) != 1:
        raise ProjectionError("capture storage receipt was not interpreted")
    sid, address_knowledge, address, fs_knowledge, presence, kind, size_knowledge, size, reason, message = stream_atoms[0].arguments
    stream_result = {
        "id": sid,
        "address": ({"knowledge": "known", "value": address}
                    if address_knowledge == "known" else
                    {"knowledge": "unknown", "reason": reason, "message": message}),
        "filesystem": ({"authority": "filesystem", "knowledge": "known",
                         "presence": presence, "kind": kind}
                        if fs_knowledge == "known" else
                        {"authority": "filesystem", "knowledge": "unknown",
                         "reason": reason, "message": message}),
        "size": ({"authority": "filesystem", "knowledge": "known", "bytes": size}
                 if size_knowledge == "known" else
                 {"authority": "filesystem", "knowledge": "unknown",
                  "reason": reason, "message": message}),
    }
    knowledge, source, segment, keep, ceiling, reason, message = policy_atoms[0].arguments
    policy_result = ({"authority": "runtime-configuration", "knowledge": "known",
                      "source": source, "segment_bytes": segment, "keep_rolls": keep,
                      "ceiling_bytes": ceiling}
                     if knowledge == "known" else
                     {"authority": "runtime-configuration", "knowledge": "unknown",
                      "reason": reason, "message": message})
    result = {"schema": adapter["output_schema"], "stream": stream_result,
              "rotation_policy": policy_result}
    validate_result(result)
    return result


def validate_result(result):
    if (not isinstance(result, dict)
            or set(result) != {"schema", "stream", "rotation_policy"}
            or result.get("schema") != "tap.capture-storage-observation/v1"):
        raise ProjectionError("invalid capture storage observation")
    stream = result["stream"]
    if not isinstance(stream, dict) or set(stream) != {"id", "address", "filesystem", "size"} or stream.get("id") != "capture_stream":
        raise ProjectionError("invalid capture stream observation")
    address, filesystem, size = stream["address"], stream["filesystem"], stream["size"]
    if not isinstance(address, dict) or address.get("knowledge") not in ("known", "unknown"):
        raise ProjectionError("invalid capture stream address")
    if ((address["knowledge"] == "known"
         and (set(address) != {"knowledge", "value"}
              or not isinstance(address["value"], str) or not address["value"]))
            or (address["knowledge"] == "unknown"
                and (set(address) != {"knowledge", "reason", "message"}
                     or address["reason"] not in ("inspection_failed", "not_observed")
                     or not isinstance(address["message"], str)))):
        raise ProjectionError("invalid capture stream address")
    if not isinstance(filesystem, dict) or filesystem.get("authority") != "filesystem" or filesystem.get("knowledge") not in ("known", "unknown"):
        raise ProjectionError("invalid capture stream filesystem observation")
    if filesystem["knowledge"] == "known":
        if (set(filesystem) != {"authority", "knowledge", "presence", "kind"}
                or filesystem["presence"] not in ("present", "absent")
                or (filesystem["presence"] == "present"
                    and filesystem["kind"] not in ("regular", "symlink", "other"))
                or (filesystem["presence"] == "absent" and filesystem["kind"] is not None)):
            raise ProjectionError("invalid capture stream filesystem observation")
    elif (set(filesystem) != {"authority", "knowledge", "reason", "message"}
          or filesystem["reason"] not in ("inspection_failed", "not_observed")
          or not isinstance(filesystem["message"], str)):
        raise ProjectionError("invalid capture stream filesystem observation")
    if not isinstance(size, dict) or size.get("authority") != "filesystem" or size.get("knowledge") not in ("known", "unknown"):
        raise ProjectionError("invalid capture stream size observation")
    if size["knowledge"] == "known":
        if set(size) != {"authority", "knowledge", "bytes"} or type(size["bytes"]) is not int or size["bytes"] < 0:
            raise ProjectionError("invalid known capture stream size")
    elif (set(size) != {"authority", "knowledge", "reason", "message"}
          or size["reason"] not in ("not_regular", "absent", "inspection_failed", "not_observed")
          or not isinstance(size["message"], str)):
        raise ProjectionError("invalid unknown capture stream size")
    policy = result["rotation_policy"]
    if not isinstance(policy, dict) or policy.get("authority") != "runtime-configuration" or policy.get("knowledge") not in ("known", "unknown"):
        raise ProjectionError("invalid capture rotation policy observation")
    if policy["knowledge"] == "known" and (policy.get("source") not in ("defaults", "profile")
            or type(policy.get("segment_bytes")) is not int
            or type(policy.get("keep_rolls")) is not int
            or policy.get("ceiling_bytes") != policy["segment_bytes"] * (policy["keep_rolls"] + 1)):
        raise ProjectionError("invalid known capture rotation policy")
    if policy["knowledge"] == "known" and set(policy) != {"authority", "knowledge", "source", "segment_bytes", "keep_rolls", "ceiling_bytes"}:
        raise ProjectionError("invalid known capture rotation policy")
    if policy["knowledge"] == "unknown" and (set(policy) != {"authority", "knowledge", "reason", "message"}
            or policy["reason"] not in ("inspection_failed", "not_observed")
            or not isinstance(policy["message"], str)):
        raise ProjectionError("invalid unknown capture rotation policy")


def observed_result(snapshot, carrier_adapter=None, sensor=inspect_sized_path,
                    observation_adapter=None):
    carrier_adapter = (load_carrier_adapter() if carrier_adapter is None
                       else carrier_adapter)
    observation_adapter = (load_adapter() if observation_adapter is None
                           else observation_adapter)
    return public_result(capture_receipts(snapshot, carrier_adapter, sensor),
                         observation_adapter)
