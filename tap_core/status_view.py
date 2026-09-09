"""Versioned status result and its optional human presentation."""
from dataclasses import dataclass
import json
from pathlib import Path

from .projection import (Atom, Derivation, ProjectionError, document_from_claims,
                         evaluate_rules, render_terminal, select_candidates,
                         validate_conservation)


MATERIAL = Path(__file__).with_name("data") / "status" / "manifest.json"
CARRIER_ADAPTER = Path(__file__).with_name("data") / "status-carrier.json"
_FRAGMENT_SCHEMA = "tap.internal-status-material-fragment/v2"
_MATERIAL_KEYS = {"schema", "id", "input_schema", "observations", "observation_collections",
                  "semantic_rules", "composition_rules", "presentation_rules",
                  "document_root", "terminal_styles"}


@dataclass(frozen=True)
class StatusProjection:
    result: dict
    meanings: tuple
    document: object
    provenance: dict


def _read_object(path, kind="status material"):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ProjectionError(f"cannot load {kind} {path.name}: {error}") from error
    if not isinstance(value, dict):
        raise ProjectionError(f"invalid {kind} object: {path.name}")
    return value


def load_material(path=MATERIAL):
    """Compose the internal status material from named, non-overlapping fragments."""
    path = Path(path)
    manifest = _read_object(path)
    if (set(manifest) != {"schema", "id", "input_schema", "fragments"}
            or manifest.get("schema") != "tap.internal-status-material-manifest/v2"
            or not isinstance(manifest.get("id"), str) or not manifest["id"]
            or not isinstance(manifest.get("input_schema"), str)
            or not isinstance(manifest.get("fragments"), list)
            or not manifest["fragments"]):
        raise ProjectionError("invalid bundled status material manifest")

    material = {
        "schema": "tap.internal-status-material/v2",
        "id": manifest["id"],
        "input_schema": manifest["input_schema"],
        "observations": {},
        "observation_collections": {},
        "semantic_rules": [],
        "composition_rules": [],
        "presentation_rules": [],
        "document_root": None,
        "terminal_styles": {},
    }
    fragment_ids = set()
    rule_ids = set()
    roots = []
    allowed = {"schema", "id", "observations", "observation_collections", "semantic_rules",
               "composition_rules", "presentation_rules", "document_root",
               "terminal_styles"}
    for name in manifest["fragments"]:
        relative = Path(name) if isinstance(name, str) else None
        if relative is None or relative.name != name or relative.suffix != ".json":
            raise ProjectionError(f"invalid status material fragment path: {name!r}")
        fragment = _read_object(path.parent / relative)
        if (not {"schema", "id"} <= set(fragment) or not set(fragment) <= allowed
                or fragment.get("schema") != _FRAGMENT_SCHEMA
                or not isinstance(fragment.get("id"), str)):
            raise ProjectionError(f"invalid status material fragment: {name}")
        if fragment["id"] in fragment_ids:
            raise ProjectionError(f"duplicate status material fragment id: {fragment['id']}")
        fragment_ids.add(fragment["id"])

        observations = fragment.get("observations", {})
        if not isinstance(observations, dict):
            raise ProjectionError(f"invalid observations in fragment: {fragment['id']}")
        overlap = (set(material["observations"]) & set(observations)) \
            | (set(material["observation_collections"]) & set(observations))
        if overlap:
            raise ProjectionError(f"duplicate observation group: {sorted(overlap)[0]}")
        material["observations"].update(observations)

        collections = fragment.get("observation_collections", {})
        if not isinstance(collections, dict):
            raise ProjectionError(f"invalid observation_collections in fragment: {fragment['id']}")
        overlap = (set(material["observation_collections"]) & set(collections)) \
            | (set(material["observations"]) & set(collections))
        if overlap:
            raise ProjectionError(f"duplicate observation group: {sorted(overlap)[0]}")
        material["observation_collections"].update(collections)

        for collection in ("semantic_rules", "composition_rules", "presentation_rules"):
            rules = fragment.get(collection, [])
            if not isinstance(rules, list):
                raise ProjectionError(f"invalid {collection} in fragment: {fragment['id']}")
            for rule in rules:
                identifier = rule.get("id") if isinstance(rule, dict) else None
                if not isinstance(identifier, str):
                    raise ProjectionError(f"invalid rule in fragment: {fragment['id']}")
                if identifier in rule_ids:
                    raise ProjectionError(f"duplicate status material rule id: {identifier}")
                rule_ids.add(identifier)
            material[collection].extend(rules)

        if "document_root" in fragment:
            if not isinstance(fragment["document_root"], str):
                raise ProjectionError(f"invalid document root in fragment: {fragment['id']}")
            roots.append(fragment["document_root"])
        styles = fragment.get("terminal_styles", {})
        if not isinstance(styles, dict) or not all(isinstance(key, str) and isinstance(value, str)
                                                  for key, value in styles.items()):
            raise ProjectionError(f"invalid terminal styles in fragment: {fragment['id']}")
        overlap = set(material["terminal_styles"]) & set(styles)
        if overlap:
            raise ProjectionError(f"duplicate terminal style: {sorted(overlap)[0]}")
        material["terminal_styles"].update(styles)

    if len(fragment_ids) != len(manifest["fragments"]):
        raise ProjectionError("status material manifest repeats a fragment")
    if len(roots) != 1:
        raise ProjectionError("status material must have exactly one document root owner")
    material["document_root"] = roots[0]
    if set(material) != _MATERIAL_KEYS or not material["observations"]:
        raise ProjectionError("invalid composed status material")
    _validate_public_shape(material["observations"], material["observation_collections"])
    return material


def _validate_path(path, label):
    if (not isinstance(path, list) or not path
            or not all(isinstance(part, str) and part for part in path)):
        raise ProjectionError(f"invalid {label}")


def _validate_source(spec):
    if (not isinstance(spec, dict) or set(spec) != {"path", "error"}
            or not isinstance(spec.get("error"), str) or not spec["error"]):
        raise ProjectionError("invalid status observation source")
    _validate_path(spec["path"], "status observation source")


def _validate_collection_source(spec):
    if (not isinstance(spec, dict) or set(spec) != {"path", "error", "fields", "max_items"}
            or not isinstance(spec.get("error"), str) or not spec["error"]
            or not isinstance(spec.get("fields"), list) or not spec["fields"]
            or type(spec.get("max_items")) is not int or spec["max_items"] < 1):
        raise ProjectionError("invalid status observation collection source")
    _validate_path(spec["path"], "status observation collection source")
    names = set()
    for field in spec["fields"]:
        if (not isinstance(field, dict) or set(field) != {"name", "path"}
                or not isinstance(field.get("name"), str) or not field["name"]
                or field["name"] in names):
            raise ProjectionError("invalid status observation collection field")
        _validate_path(field["path"], "status observation collection field")
        names.add(field["name"])


def _validate_public_shape(observations, collections):
    if not isinstance(observations, dict) or not observations:
        raise ProjectionError("invalid status observation vocabulary")
    if not isinstance(collections, dict):
        raise ProjectionError("invalid status observation collection vocabulary")
    overlap = set(observations) & set(collections)
    if overlap:
        raise ProjectionError(f"duplicate observation group: {sorted(overlap)[0]}")
    for group, names in observations.items():
        if (not isinstance(group, str) or not group or not isinstance(names, list) or not names
                or not all(isinstance(name, str) and name for name in names)
                or len(names) != len(set(names))):
            raise ProjectionError(f"invalid observation vocabulary: {group}")
    for group, declaration in collections.items():
        if (not isinstance(group, str) or not group or not isinstance(declaration, dict)
                or set(declaration) != {"fields", "max_items"}
                or not isinstance(declaration["fields"], list) or not declaration["fields"]
                or not all(isinstance(name, str) and name for name in declaration["fields"])
                or len(declaration["fields"]) != len(set(declaration["fields"]))
                or type(declaration["max_items"]) is not int or declaration["max_items"] < 1):
            raise ProjectionError(f"invalid observation collection vocabulary: {group}")


def load_carrier_adapter(path=CARRIER_ADAPTER):
    """Load and validate the physical snapshot-to-result mapping."""
    adapter = _read_object(Path(path), "status carrier adapter")
    _validate_carrier_adapter(adapter)
    return adapter


def _validate_carrier_adapter(adapter):
    if (not isinstance(adapter, dict)
            or set(adapter) != {"schema", "id", "output_schema", "profile_path", "errors_path",
                        "observations", "observation_collections"}
            or adapter.get("schema") != "tap.status-snapshot-adapter/v1"
            or not isinstance(adapter.get("id"), str) or not adapter["id"]
            or not isinstance(adapter.get("output_schema"), str) or not adapter["output_schema"]):
        raise ProjectionError("invalid status carrier adapter")
    _validate_path(adapter["profile_path"], "status carrier profile path")
    _validate_path(adapter["errors_path"], "status carrier errors path")
    observations = adapter["observations"]
    collections = adapter["observation_collections"]
    if not isinstance(observations, dict) or not isinstance(collections, dict):
        raise ProjectionError("invalid status carrier adapter declarations")
    if set(observations) & set(collections):
        raise ProjectionError("duplicate status carrier target group")
    for group, declarations in observations.items():
        if (not isinstance(group, str) or not group or not isinstance(declarations, list)
                or not declarations):
            raise ProjectionError("invalid status carrier observation group")
        names = set()
        for declaration in declarations:
            if (not isinstance(declaration, dict) or set(declaration) != {"name", "source"}
                    or not isinstance(declaration.get("name"), str) or not declaration["name"]
                    or declaration["name"] in names):
                raise ProjectionError("invalid status carrier observation declaration")
            names.add(declaration["name"])
            _validate_source(declaration["source"])
    for group, declaration in collections.items():
        if not isinstance(group, str) or not group:
            raise ProjectionError("invalid status carrier collection group")
        _validate_collection_source(declaration)


def observe(spec, snapshot, errors):
    """Read one declared observation from a supplied snapshot carrier."""
    _validate_source(spec)
    if not isinstance(errors, dict):
        raise ProjectionError("status carrier errors must be an object")
    if spec["error"] in errors:
        return {"knowledge": "unknown", "reason": "inspection_failed",
                "message": errors[spec["error"]]}

    return _observe_path(spec["path"], snapshot)


def _observe_path(path, carrier):
    value = carrier
    for part in path:
        if not isinstance(value, dict) or part not in value:
            return {"knowledge": "unknown", "reason": "not_observed",
                    "message": "observation was not supplied"}
        value = value[part]
    return {"knowledge": "known", "value": value}


def observe_collection(spec, snapshot, errors):
    """Normalize one declared named-map observation without interpreting its items."""
    _validate_collection_source(spec)
    collection = observe({"path": spec["path"], "error": spec["error"]}, snapshot, errors)
    if collection["knowledge"] == "unknown":
        return collection
    value = collection["value"]
    if not isinstance(value, dict) or not all(isinstance(name, str) and name for name in value):
        raise ProjectionError("status observation collection must be a named map")
    if len(value) > spec["max_items"]:
        raise ProjectionError("status observation collection exceeds its declared bound")

    items = []
    for ordinal, name in enumerate(sorted(value), 1):
        row = value[name]
        if not isinstance(row, dict):
            raise ProjectionError("status observation collection item must be an object")
        observations = {
            field["name"]: _observe_path(field["path"], row)
            for field in spec["fields"]
        }
        items.append({"name": name, "ordinal": ordinal, "observations": observations})
    return {"knowledge": "known", "items": items}


def _carrier_value(path, carrier, default):
    value = carrier
    for part in path:
        if not isinstance(value, dict) or part not in value:
            return default
        value = value[part]
    return value


def public_status_result(snapshot, adapter=None):
    """Reduce the operational snapshot to the stable, compact public contract."""
    adapter = load_carrier_adapter() if adapter is None else adapter
    _validate_carrier_adapter(adapter)
    errors = _carrier_value(adapter["errors_path"], snapshot, {})
    if not isinstance(errors, dict):
        raise ProjectionError("status carrier errors must be an object")
    result = {"schema": adapter["output_schema"],
              "profile": _carrier_value(adapter["profile_path"], snapshot, None)}
    for group, declarations in adapter["observations"].items():
        result[group] = {item["name"]: observe(item["source"], snapshot, errors)
                         for item in declarations}
    for group, declaration in adapter["observation_collections"].items():
        result[group] = observe_collection(declaration, snapshot, errors)
    return result


def validate_status_result(result, material=None):
    material = load_material() if material is None else material
    if not isinstance(result, dict) or result.get("schema") != material["input_schema"]:
        raise ProjectionError("unsupported status result")
    if set(result) != {"schema", "profile", *material["observations"],
                       *material["observation_collections"]}:
        raise ProjectionError("invalid status result fields")
    for group, declarations in material["observations"].items():
        expected = set(declarations)
        if not isinstance(result[group], dict) or set(result[group]) != expected:
            raise ProjectionError(f"invalid {group} observations")
        for observation in result[group].values():
            if not isinstance(observation, dict) or observation.get("knowledge") not in ("known", "unknown"):
                raise ProjectionError("invalid observation")
            if observation["knowledge"] == "known" and set(observation) != {"knowledge", "value"}:
                raise ProjectionError("invalid known observation")
            if observation["knowledge"] == "unknown":
                if (set(observation) != {"knowledge", "reason", "message"}
                        or observation["reason"] not in ("inspection_failed", "not_observed")
                        or not isinstance(observation["message"], str)):
                    raise ProjectionError("invalid unknown observation")
    for group, declaration in material["observation_collections"].items():
        collection = result[group]
        if not isinstance(collection, dict) or collection.get("knowledge") not in ("known", "unknown"):
            raise ProjectionError("invalid observation collection")
        if collection["knowledge"] == "unknown":
            if (set(collection) != {"knowledge", "reason", "message"}
                    or collection["reason"] not in ("inspection_failed", "not_observed")
                    or not isinstance(collection["message"], str)):
                raise ProjectionError("invalid unknown observation collection")
            continue
        if (set(collection) != {"knowledge", "items"} or not isinstance(collection["items"], list)
                or len(collection["items"]) > declaration["max_items"]):
            raise ProjectionError("invalid known observation collection")
        expected = set(declaration["fields"])
        previous = None
        for ordinal, item in enumerate(collection["items"], 1):
            if (not isinstance(item, dict) or set(item) != {"name", "ordinal", "observations"}
                    or not isinstance(item["name"], str) or not item["name"]
                    or item["ordinal"] != ordinal or (previous is not None and item["name"] <= previous)
                    or not isinstance(item["observations"], dict)
                    or set(item["observations"]) != expected):
                raise ProjectionError("invalid observation collection item")
            previous = item["name"]
            for observation in item["observations"].values():
                if not isinstance(observation, dict) or observation.get("knowledge") not in ("known", "unknown"):
                    raise ProjectionError("invalid collection item observation")
                if observation["knowledge"] == "known" and set(observation) != {"knowledge", "value"}:
                    raise ProjectionError("invalid known collection item observation")
                if observation["knowledge"] == "unknown" and (
                        set(observation) != {"knowledge", "reason", "message"}
                        or observation["reason"] not in ("inspection_failed", "not_observed")
                        or not isinstance(observation["message"], str)):
                    raise ProjectionError("invalid unknown collection item observation")


def _facts(result, material):
    claims = {}
    for group in material["observations"]:
        observations = result[group]
        for name, observation in observations.items():
            value = observation.get("value", observation.get("reason"))
            atom = Atom("observation", (group, name, observation["knowledge"], value))
            claims[atom] = Derivation("supplied-status-result", tuple())
    for group in material["observation_collections"]:
        collection = result[group]
        value = len(collection["items"]) if collection["knowledge"] == "known" else collection["reason"]
        atom = Atom("collection-observation", (group, collection["knowledge"], value))
        claims[atom] = Derivation("supplied-status-result", tuple())
        if collection["knowledge"] == "unknown":
            continue
        for item in collection["items"]:
            for name, observation in item["observations"].items():
                value = observation.get("value", observation.get("reason"))
                atom = Atom.from_value(["observation-item", group, item["name"], item["ordinal"],
                                        name, observation["knowledge"], value])
                claims[atom] = Derivation("supplied-status-result", tuple())
    return claims


def project_status(result, material=None):
    """Run the two explicit chains: observations to meanings, meanings to slots."""
    material = load_material() if material is None else material
    validate_status_result(result, material)
    semantic = evaluate_rules(_facts(result, material), material["semantic_rules"])
    section_selected = select_candidates(semantic)
    composed = evaluate_rules({**semantic, **section_selected}, material["composition_rules"])
    selected = select_candidates(composed)
    meanings = tuple(sorted((atom for atom in selected if atom.relation == "meaning"),
                            key=Atom.identifier))
    interpreted = {atom: derivation for atom, derivation in composed.items()
                   if atom.relation != "meaning"}
    interpreted.update(selected)
    presented = evaluate_rules(interpreted, material["presentation_rules"])
    trace = {**composed, **selected, **presented}
    document = document_from_claims(trace, material["document_root"])
    validate_conservation(trace, meanings, document, "supplied-status-result")
    return StatusProjection(result, meanings, document, trace)


def terminal_status(result, width=80, color=False, material=None):
    material = load_material() if material is None else material
    return render_terminal(project_status(result, material).document, width, color,
                           material["terminal_styles"])
