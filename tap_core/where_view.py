"""Versioned address result and optional human presentation for ``tap where``."""
from dataclasses import dataclass
import json
from pathlib import Path

from .material_view import observe, project_claims
from .projection import Atom, Derivation, ProjectionError, render_terminal


MATERIAL = Path(__file__).with_name("data") / "where.json"
CARRIER_ADAPTER = Path(__file__).with_name("data") / "where-carrier.json"


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


def load_material(path=MATERIAL):
    value = _read_object(path, "where material")
    required = {"schema", "id", "input_schema", "document_root", "sections", "locations",
                "semantic_rules", "presentation_rules", "terminal_styles"}
    if (set(value) != required
            or value.get("schema") != "tap.internal-where-material/v2"
            or not isinstance(value.get("id"), str) or not value["id"]
            or value.get("input_schema") != "tap.where-result/v1"
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


def validate_where_result(result, material=None):
    material = load_material() if material is None else material
    expected = {location["id"] for location in material["locations"]}
    if (not isinstance(result, dict) or set(result) != {"schema", "addresses"}
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
    return claims


def project_where(result, material=None):
    material = load_material() if material is None else material
    validate_where_result(result, material)
    projection = project_claims(_facts(result, material), material,
                                "supplied-where-result")
    return WhereProjection(result, projection.meanings, projection.document,
                           projection.provenance)


def terminal_where(result, width=None, color=False, material=None):
    material = load_material() if material is None else material
    limit = 2 ** 31 - 1 if width is None else width
    return render_terminal(project_where(result, material).document, limit, color,
                           material["terminal_styles"])
