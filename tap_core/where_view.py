"""Versioned address result and optional human presentation for ``tap where``."""
from dataclasses import dataclass
import json
from pathlib import Path

from .capture_storage_view import (observed_result as observed_capture_storage,
                                   validate_result as validate_capture_storage)
from .filesystem_sensor import inspect_path
from .material_view import observe, project_claims
from .projection import (Atom, Derivation, ProjectionError, evaluate_rules,
                         render_terminal)


MATERIAL = Path(__file__).with_name("data") / "where.json"
CARRIER_ADAPTER = Path(__file__).with_name("data") / "where-carrier.json"
FILESYSTEM_ADAPTER = Path(__file__).with_name("data") / "where-filesystem.json"
PRESENCE_MATERIAL = Path(__file__).with_name("data") / "where-presence.json"
STORAGE_MATERIAL = Path(__file__).with_name("data") / "where-storage.json"


@dataclass(frozen=True)
class WhereProjection:
    result: dict
    meanings: tuple
    document: object
    provenance: dict


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
        raise ProjectionError("invalid where carrier source") from error


def _validate_carrier_adapter(adapter):
    observations = adapter.get("observations") if isinstance(adapter, dict) else None
    if (not isinstance(adapter, dict)
            or set(adapter) != {"schema", "id", "output_schema", "errors_path", "observations"}
            or adapter.get("schema") != "tap.where-snapshot-adapter/v1"
            or not isinstance(adapter.get("id"), str) or not adapter["id"]
            or adapter.get("output_schema") != "tap.where-result/v1"
            or not isinstance(adapter.get("errors_path"), list)
            or not adapter["errors_path"]
            or not all(isinstance(part, str) and part for part in adapter["errors_path"])
            or not isinstance(observations, dict) or set(observations) != {"addresses"}):
        raise ProjectionError("invalid where carrier adapter")
    declarations = observations["addresses"]
    if not isinstance(declarations, list) or not declarations:
        raise ProjectionError("invalid where carrier observations")
    names = set()
    for declaration in declarations:
        if (not isinstance(declaration, dict) or set(declaration) != {"name", "source"}
                or not isinstance(declaration.get("name"), str) or not declaration["name"]
                or declaration["name"] in names):
            raise ProjectionError("invalid where carrier observation")
        names.add(declaration["name"])
        _validate_source(declaration["source"])


def load_carrier_adapter(path=CARRIER_ADAPTER):
    adapter = _read_object(path, "where carrier adapter")
    _validate_carrier_adapter(adapter)
    return adapter


def _validate_filesystem_adapter(adapter):
    required = {"schema", "id", "receipt_schema", "output_schema", "targets",
                "semantic_rules"}
    if (not isinstance(adapter, dict) or set(adapter) != required
            or adapter.get("schema") != "tap.where-filesystem-adapter/v1"
            or not isinstance(adapter.get("id"), str) or not adapter["id"]
            or adapter.get("receipt_schema") != "tap.filesystem-sensor-receipts/v1"
            or adapter.get("output_schema") != "tap.filesystem-observation/v1"
            or not isinstance(adapter.get("targets"), list) or not adapter["targets"]
            or not isinstance(adapter.get("semantic_rules"), list)):
        raise ProjectionError("invalid where filesystem adapter")
    names = set()
    for target in adapter["targets"]:
        if (not isinstance(target, dict) or set(target) != {"name", "address_source"}
                or not isinstance(target.get("name"), str) or not target["name"]
                or target["name"] in names):
            raise ProjectionError("invalid where filesystem target")
        names.add(target["name"])
        _validate_source(target["address_source"])
    rule_ids = set()
    for rule in adapter["semantic_rules"]:
        identifier = rule.get("id") if isinstance(rule, dict) else None
        if not isinstance(identifier, str) or not identifier or identifier in rule_ids:
            raise ProjectionError("invalid where filesystem rule")
        rule_ids.add(identifier)


def load_filesystem_adapter(path=FILESYSTEM_ADAPTER):
    adapter = _read_object(path, "where filesystem adapter")
    _validate_filesystem_adapter(adapter)
    return adapter


def capture_filesystem_receipts(snapshot, adapter=None, sensor=inspect_path):
    """Run declared bounded sensors and retain their physical receipts."""
    adapter = load_filesystem_adapter() if adapter is None else adapter
    _validate_filesystem_adapter(adapter)
    errors = snapshot.get("inspection_errors", {}) if isinstance(snapshot, dict) else {}
    receipts = {}
    for target in adapter["targets"]:
        address = observe(target["address_source"], snapshot, errors)
        if address["knowledge"] == "known":
            if not isinstance(address["value"], str) or not address["value"]:
                raise ProjectionError("invalid filesystem target address")
            receipt = sensor(address["value"])
        else:
            receipt = {"outcome": "not_observed", "kind": None, "errno": None,
                       "message": address["message"]}
        receipts[target["name"]] = receipt
    return {"schema": adapter["receipt_schema"], "receipts": receipts}


def _validate_receipt(receipt):
    if (not isinstance(receipt, dict)
            or set(receipt) != {"outcome", "kind", "errno", "message"}
            or receipt.get("outcome") not in ("present", "absent", "failed", "not_observed")
            or not isinstance(receipt.get("message"), str)
            or (receipt.get("errno") is not None and type(receipt["errno"]) is not int)):
        raise ProjectionError("invalid filesystem sensor receipt")
    if receipt["outcome"] == "present":
        if receipt["kind"] not in ("regular", "symlink", "other"):
            raise ProjectionError("invalid present filesystem receipt")
    elif receipt["kind"] is not None:
        raise ProjectionError("invalid non-present filesystem receipt")


def _validate_filesystem_observation(observation):
    if (not isinstance(observation, dict)
            or observation.get("authority") != "filesystem"
            or observation.get("knowledge") not in ("known", "unknown")):
        raise ProjectionError("invalid where filesystem observation")
    if observation["knowledge"] == "known":
        if (set(observation) != {"authority", "knowledge", "presence", "kind"}
                or observation["presence"] not in ("present", "absent")
                or (observation["presence"] == "present"
                    and observation["kind"] not in ("regular", "symlink", "other"))
                or (observation["presence"] == "absent"
                    and observation["kind"] is not None)):
            raise ProjectionError("invalid known filesystem observation")
    elif (set(observation) != {"authority", "knowledge", "reason", "message"}
          or observation["reason"] not in ("inspection_failed", "not_observed")
          or not isinstance(observation["message"], str)):
        raise ProjectionError("invalid unknown filesystem observation")


def validate_filesystem_result(result, expected=None):
    if (not isinstance(result, dict)
            or set(result) != {"schema", "observations"}
            or result.get("schema") != "tap.filesystem-observation/v1"
            or not isinstance(result.get("observations"), dict)
            or not result["observations"]
            or (expected is not None and set(result["observations"]) != set(expected))):
        raise ProjectionError("invalid public filesystem result")
    for observation in result["observations"].values():
        _validate_filesystem_observation(observation)


def public_filesystem_result(receipt_carrier, adapter=None):
    """Translate sensor receipts to public filesystem observations by rules."""
    adapter = load_filesystem_adapter() if adapter is None else adapter
    _validate_filesystem_adapter(adapter)
    expected = {target["name"] for target in adapter["targets"]}
    if (not isinstance(receipt_carrier, dict)
            or set(receipt_carrier) != {"schema", "receipts"}
            or receipt_carrier.get("schema") != adapter["receipt_schema"]
            or not isinstance(receipt_carrier.get("receipts"), dict)
            or set(receipt_carrier["receipts"]) != expected):
        raise ProjectionError("invalid filesystem receipt carrier")
    facts = {}
    for identifier, receipt in receipt_carrier["receipts"].items():
        _validate_receipt(receipt)
        atom = Atom.from_value(["filesystem-receipt", identifier, receipt["outcome"],
                                receipt["kind"], receipt["errno"], receipt["message"]])
        facts[atom] = Derivation("supplied-filesystem-receipt", tuple())
    projected = evaluate_rules(facts, adapter["semantic_rules"])
    observations = {}
    for atom in projected:
        if atom.relation != "filesystem-observation":
            continue
        if len(atom.arguments) != 7:
            raise ProjectionError("invalid projected filesystem observation")
        identifier, authority, knowledge, presence, kind, reason, message = atom.arguments
        if identifier in observations:
            raise ProjectionError("ambiguous filesystem observation")
        value = {"authority": authority, "knowledge": knowledge}
        if knowledge == "known":
            value.update({"presence": presence, "kind": kind})
        else:
            value.update({"reason": reason, "message": message})
        observations[identifier] = value
    result = {"schema": adapter["output_schema"], "observations": observations}
    try:
        validate_filesystem_result(result, expected)
    except ProjectionError as error:
        raise ProjectionError("filesystem receipt was not interpreted") from error
    return result


def load_material(path=MATERIAL):
    value = _read_object(path, "where material")
    if value.get("schema") in ("tap.internal-where-material-overlay/v1",
                               "tap.internal-where-material-overlay/v2"):
        structural = value["schema"].endswith("/v2")
        required_overlay = {"schema", "id", "extends", "effective_schema",
                            "input_schema", "semantic_rules", "presentation_rules",
                            "terminal_styles"}
        if structural:
            required_overlay |= {"sections", "locations"}
        base_name = value.get("extends")
        supported = {
            ("tap.internal-where-material/v3", "tap.where-result/v2"),
            ("tap.internal-where-material/v4", "tap.where-result/v3"),
        }
        if (set(value) != required_overlay
                or not isinstance(value.get("id"), str) or not value["id"]
                or not isinstance(base_name, str) or not base_name
                or Path(base_name).name != base_name or Path(base_name).suffix != ".json"
                or base_name == Path(path).name
                or (value.get("effective_schema"), value.get("input_schema")) not in supported
                or not isinstance(value.get("semantic_rules"), list)
                or not isinstance(value.get("presentation_rules"), list)
                or not isinstance(value.get("terminal_styles"), dict)
                or (structural and (not isinstance(value.get("sections"), list)
                                    or not isinstance(value.get("locations"), list)))):
            raise ProjectionError("invalid bundled where material overlay")
        base = load_material(Path(path).parent / base_name)
        value = {
            **base,
            "schema": value["effective_schema"],
            "id": value["id"],
            "input_schema": value["input_schema"],
            "sections": [*base["sections"], *value.get("sections", [])],
            "locations": [*base["locations"], *value.get("locations", [])],
            "semantic_rules": [*base["semantic_rules"], *value["semantic_rules"]],
            "presentation_rules": [*base["presentation_rules"],
                                   *value["presentation_rules"]],
            "terminal_styles": {**base["terminal_styles"],
                                **value["terminal_styles"]},
        }
    required = {"schema", "id", "input_schema", "document_root", "sections", "locations",
                "semantic_rules", "presentation_rules", "terminal_styles"}
    material_inputs = {
        "tap.internal-where-material/v2": "tap.where-result/v1",
        "tap.internal-where-material/v3": "tap.where-result/v2",
        "tap.internal-where-material/v4": "tap.where-result/v3",
    }
    if (set(value) != required
            or value.get("schema") not in material_inputs
            or not isinstance(value.get("id"), str) or not value["id"]
            or value.get("input_schema") != material_inputs.get(value.get("schema"))
            or value.get("document_root") != "where"
            or not isinstance(value.get("sections"), list)
            or not isinstance(value.get("locations"), list)
            or not isinstance(value.get("semantic_rules"), list)
            or not isinstance(value.get("presentation_rules"), list)
            or not isinstance(value.get("terminal_styles"), dict)):
        raise ProjectionError("invalid bundled where material")
    section_ids = set()
    section_orders = set()
    for section in value["sections"]:
        if (not isinstance(section, dict) or set(section) != {"id", "label", "order"}
                or not isinstance(section["id"], str) or not section["id"]
                or not isinstance(section["label"], str) or not section["label"]
                or type(section["order"]) is not int or section["order"] < 0
                or section["id"] in section_ids or section["order"] in section_orders):
            raise ProjectionError("invalid where material section")
        section_ids.add(section["id"])
        section_orders.add(section["order"])
    location_ids = set()
    sibling_orders = set()
    for location in value["locations"]:
        expected = {"id", "label", "section", "order", "role", "owner"}
        if (not isinstance(location, dict) or set(location) != expected
                or not all(isinstance(location[name], str) and location[name]
                           for name in ("id", "label", "section", "role", "owner"))
                or location["section"] not in section_ids
                or type(location["order"]) is not int or location["order"] < 1
                or location["id"] in location_ids
                or (location["section"], location["order"]) in sibling_orders):
            raise ProjectionError("invalid where material location")
        location_ids.add(location["id"])
        sibling_orders.add((location["section"], location["order"]))
    if not section_ids or not location_ids:
        raise ProjectionError("where material must declare sections and locations")
    rule_ids = set()
    for collection in ("semantic_rules", "presentation_rules"):
        for rule in value[collection]:
            identifier = rule.get("id") if isinstance(rule, dict) else None
            if not isinstance(identifier, str) or not identifier or identifier in rule_ids:
                raise ProjectionError("invalid or duplicate where material rule")
            rule_ids.add(identifier)
    if not all(isinstance(name, str) and isinstance(style, str)
               for name, style in value["terminal_styles"].items()):
        raise ProjectionError("invalid where terminal styles")
    return value


def _ordered_locations(material):
    sections = {section["id"]: section["order"] for section in material["sections"]}
    return sorted(material["locations"],
                  key=lambda item: (sections[item["section"]], item["order"], item["id"]))


def public_where_result(snapshot, adapter=None):
    """Reduce a physical operation carrier to address observations only."""
    adapter = load_carrier_adapter() if adapter is None else adapter
    _validate_carrier_adapter(adapter)
    errors = snapshot
    for part in adapter["errors_path"]:
        if not isinstance(errors, dict) or part not in errors:
            errors = {}
            break
        errors = errors[part]
    if not isinstance(errors, dict):
        raise ProjectionError("where carrier errors must be an object")
    return {
        "schema": adapter["output_schema"],
        "addresses": {
            declaration["name"]: observe(declaration["source"], snapshot, errors)
            for declaration in adapter["observations"]["addresses"]
        },
    }


def with_filesystem_observations(address_result, filesystem_result):
    """Join two public results without exposing either physical carrier."""
    validate_where_result(address_result, load_material(MATERIAL))
    validate_filesystem_result(filesystem_result)
    if not set(filesystem_result["observations"]).issubset(address_result["addresses"]):
        raise ProjectionError("filesystem observation has no declared address")
    return {
        "schema": "tap.where-result/v2",
        "addresses": address_result["addresses"],
        "filesystem": filesystem_result["observations"],
    }


def observed_where_result(snapshot, carrier_adapter=None, filesystem_adapter=None,
                          sensor=inspect_path):
    """Run the bounded where observation chain used by semantic presentation."""
    address_result = public_where_result(snapshot, carrier_adapter)
    filesystem_adapter = (load_filesystem_adapter() if filesystem_adapter is None
                          else filesystem_adapter)
    receipts = capture_filesystem_receipts(snapshot, filesystem_adapter, sensor)
    filesystem = public_filesystem_result(receipts, filesystem_adapter)
    return with_filesystem_observations(address_result, filesystem)


def storage_where_result(snapshot, carrier_adapter=None, filesystem_adapter=None,
                         filesystem_sensor=inspect_path, storage_carrier_adapter=None,
                         storage_sensor=None):
    """Add bounded Core capture storage observations to the established v2 result."""
    base = observed_where_result(snapshot, carrier_adapter, filesystem_adapter,
                                 filesystem_sensor)
    arguments = {} if storage_sensor is None else {"sensor": storage_sensor}
    storage = observed_capture_storage(snapshot, storage_carrier_adapter, **arguments)
    return with_capture_storage(base, storage)


def with_capture_storage(result, storage):
    validate_where_result(result, load_material(PRESENCE_MATERIAL))
    validate_capture_storage(storage)
    return {"schema": "tap.where-result/v3", "addresses": result["addresses"],
            "filesystem": result["filesystem"], "capture_storage": storage}


def _material_for(result):
    schema = result.get("schema") if isinstance(result, dict) else None
    if schema == "tap.where-result/v3":
        return load_material(STORAGE_MATERIAL)
    if schema == "tap.where-result/v2":
        return load_material(PRESENCE_MATERIAL)
    return load_material(MATERIAL)


def validate_where_result(result, material=None):
    if material is None:
        material = _material_for(result)
    expected = {location["id"] for location in material["locations"]}
    fields = {
        "tap.where-result/v1": {"schema", "addresses"},
        "tap.where-result/v2": {"schema", "addresses", "filesystem"},
        "tap.where-result/v3": {"schema", "addresses", "filesystem", "capture_storage"},
    }[material["input_schema"]]
    if (not isinstance(result, dict) or set(result) != fields
            or result.get("schema") != material["input_schema"]
            or not isinstance(result.get("addresses"), dict)
            or set(result["addresses"]) != expected):
        raise ProjectionError("invalid where result")
    for address in result["addresses"].values():
        if not isinstance(address, dict) or address.get("knowledge") not in ("known", "unknown"):
            raise ProjectionError("invalid where address observation")
        if address["knowledge"] == "known":
            if (set(address) != {"knowledge", "value"}
                    or not isinstance(address["value"], str) or not address["value"]):
                raise ProjectionError("invalid known where address")
        elif (set(address) != {"knowledge", "reason", "message"}
              or address["reason"] not in ("inspection_failed", "not_observed")
              or not isinstance(address["message"], str)):
            raise ProjectionError("invalid unknown where address")
    if "filesystem" in result:
        if (not isinstance(result["filesystem"], dict) or not result["filesystem"]
                or not set(result["filesystem"]).issubset(expected)):
            raise ProjectionError("invalid where filesystem observations")
        for observation in result["filesystem"].values():
            _validate_filesystem_observation(observation)
    if "capture_storage" in result:
        validate_capture_storage(result["capture_storage"])


def _human_bytes(value):
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    amount = float(value)
    unit = units[0]
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            break
        amount /= 1024
    text = str(int(amount)) if amount.is_integer() else f"{amount:.1f}".rstrip("0").rstrip(".")
    return f"{text} {unit}"


def _facts(result, material):
    claims = {Atom.from_value(["document", material["document_root"]]):
              Derivation("bundled-where-material", tuple())}
    for section in material["sections"]:
        atom = Atom.from_value(["section", section["id"], section["label"], section["order"]])
        claims[atom] = Derivation("bundled-where-material", tuple())
    for declaration in _ordered_locations(material):
        address = result["addresses"][declaration["id"]]
        observed = address.get("value", address.get("reason"))
        atom = Atom.from_value([
            "location-observation", declaration["id"], declaration["section"],
            declaration["role"], declaration["owner"], declaration["label"],
            declaration["order"], address["knowledge"], observed,
        ])
        claims[atom] = Derivation("supplied-where-result", tuple())
    for identifier, observation in result.get("filesystem", {}).items():
        atom = Atom.from_value([
            "filesystem-observation", identifier, observation["authority"],
            observation["knowledge"], observation.get("presence"),
            observation.get("kind"), observation.get("reason"),
            observation.get("message", ""),
        ])
        claims[atom] = Derivation("supplied-where-result", tuple())
    storage = result.get("capture_storage")
    if storage is not None:
        stream = storage["stream"]
        address = stream["address"]
        address_atom = Atom.from_value([
            "capture-stream-address", stream["id"], address["knowledge"],
            address.get("value"), address.get("reason"), address.get("message", ""),
        ])
        filesystem = stream["filesystem"]
        filesystem_atom = Atom.from_value([
            "capture-stream-filesystem", stream["id"], filesystem["authority"],
            filesystem["knowledge"], filesystem.get("presence"), filesystem.get("kind"),
            filesystem.get("reason"), filesystem.get("message", ""),
        ])
        size = stream["size"]
        size_atom = Atom.from_value([
            "capture-stream-size", stream["id"], size["authority"], size["knowledge"],
            size.get("bytes"), size.get("reason"), size.get("message", ""),
        ])
        policy = storage["rotation_policy"]
        policy_atom = Atom.from_value([
            "capture-rotation-policy", policy["authority"], policy["knowledge"],
            policy.get("source"), policy.get("segment_bytes"), policy.get("keep_rolls"),
            policy.get("ceiling_bytes"), policy.get("reason"), policy.get("message", ""),
        ])
        for atom in (address_atom, filesystem_atom, size_atom, policy_atom):
            claims[atom] = Derivation("supplied-where-result", tuple())
        if size["knowledge"] == "known":
            display = Atom.from_value(["byte-quantity", "capture_stream", "size",
                                       _human_bytes(size["bytes"])])
            claims[display] = Derivation("format-byte-quantity", (size_atom,))
        if policy["knowledge"] == "known":
            for name in ("segment_bytes", "ceiling_bytes"):
                display = Atom.from_value(["byte-quantity", "capture_rotation", name,
                                           _human_bytes(policy[name])])
                claims[display] = Derivation("format-byte-quantity", (policy_atom,))
    return claims


def project_where(result, material=None):
    if material is None:
        material = _material_for(result)
    validate_where_result(result, material)
    projection = project_claims(_facts(result, material), material,
                                "supplied-where-result")
    return WhereProjection(result, projection.meanings, projection.document,
                           projection.provenance)


def terminal_where(result, width=None, color=False, material=None):
    if material is None:
        material = _material_for(result)
    limit = 2 ** 31 - 1 if width is None else width
    return render_terminal(project_where(result, material).document, limit, color,
                           material["terminal_styles"])
