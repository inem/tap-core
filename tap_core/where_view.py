"""Versioned address result and optional human presentation for ``tap where``."""
from dataclasses import dataclass
import json
from pathlib import Path

from .material_view import observe, project_claims
from .projection import Atom, Derivation, ProjectionError, render_terminal


MATERIAL = Path(__file__).with_name("data") / "where.json"


@dataclass(frozen=True)
class WhereProjection:
    result: dict
    meanings: tuple
    document: object
    provenance: dict


def load_material(path=MATERIAL):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ProjectionError(f"cannot load where material: {error}") from error
    required = {"schema", "result_schema", "document_root", "sections", "locations",
                "semantic_rules", "presentation_rules", "terminal_styles"}
    if (not isinstance(value, dict) or set(value) != required
            or value.get("schema") != "tap.internal-where-material/v1"
            or value.get("result_schema") != "tap.where-result/v1"
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
        expected = {"id", "label", "section", "order", "role", "owner", "source"}
        if (not isinstance(location, dict) or set(location) != expected
                or not all(isinstance(location[name], str) and location[name]
                           for name in ("id", "label", "section", "role", "owner"))
                or location["section"] not in section_ids
                or type(location["order"]) is not int or location["order"] < 1
                or location["id"] in location_ids
                or (location["section"], location["order"]) in sibling_orders):
            raise ProjectionError("invalid where material location")
        try:
            observe(location["source"], {})
        except ProjectionError as error:
            raise ProjectionError("invalid where material location source") from error
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


def public_where_result(snapshot, material=None):
    """Turn current raw addresses into a versioned address-only result."""
    material = load_material() if material is None else material
    locations = []
    for declaration in _ordered_locations(material):
        locations.append({
            "id": declaration["id"],
            "section": declaration["section"],
            "role": declaration["role"],
            "owner": declaration["owner"],
            "label": declaration["label"],
            "order": declaration["order"],
            "address": observe(declaration["source"], snapshot),
        })
    return {"schema": material["result_schema"], "locations": locations}


def validate_where_result(result, material=None):
    material = load_material() if material is None else material
    if (not isinstance(result, dict) or set(result) != {"schema", "locations"}
            or result.get("schema") != material["result_schema"]
            or not isinstance(result.get("locations"), list)
            or len(result["locations"]) != len(material["locations"])):
        raise ProjectionError("invalid where result")
    for item, declaration in zip(result["locations"], _ordered_locations(material)):
        metadata = {"id", "section", "role", "owner", "label", "order"}
        if (not isinstance(item, dict) or set(item) != {*metadata, "address"}
                or any(item[name] != declaration[name] for name in metadata)):
            raise ProjectionError("invalid where result location")
        address = item["address"]
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
    claims = {
        Atom.from_value(["document", material["document_root"]]):
            Derivation("bundled-where-material", tuple())
    }
    for section in material["sections"]:
        atom = Atom.from_value(["section", section["id"], section["label"], section["order"]])
        claims[atom] = Derivation("bundled-where-material", tuple())
    for item in result["locations"]:
        address = item["address"]
        value = address.get("value", address.get("reason"))
        atom = Atom.from_value([
            "location-observation", item["id"], item["section"], item["role"],
            item["owner"], item["label"], item["order"], address["knowledge"], value,
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
    # Addresses are required content. With no explicit width constraint they are
    # emitted intact; a caller-supplied width is checked by the generic renderer.
    limit = 2 ** 31 - 1 if width is None else width
    return render_terminal(project_where(result, material).document, limit, color,
                           material["terminal_styles"])
