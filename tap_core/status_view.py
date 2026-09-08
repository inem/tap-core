"""Versioned status result and its optional human presentation."""
from dataclasses import dataclass
import json
from pathlib import Path

from .projection import (Atom, Derivation, ProjectionError, document_from_claims,
                         evaluate_rules, render_terminal, select_candidates,
                         validate_conservation)


MATERIAL = Path(__file__).with_name("data") / "status" / "manifest.json"
_FRAGMENT_SCHEMA = "tap.internal-status-material-fragment/v1"
_MATERIAL_KEYS = {"schema", "result_schema", "observations", "semantic_rules",
                  "composition_rules", "presentation_rules", "document_root",
                  "terminal_styles"}


@dataclass(frozen=True)
class StatusProjection:
    result: dict
    meanings: tuple
    document: object
    provenance: dict


def _read_object(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ProjectionError(f"cannot load status material {path.name}: {error}") from error
    if not isinstance(value, dict):
        raise ProjectionError(f"invalid status material object: {path.name}")
    return value


def load_material(path=MATERIAL):
    """Compose the internal status material from named, non-overlapping fragments."""
    path = Path(path)
    manifest = _read_object(path)
    if (set(manifest) != {"schema", "result_schema", "fragments"}
            or manifest.get("schema") != "tap.internal-status-material-manifest/v1"
            or not isinstance(manifest.get("result_schema"), str)
            or not isinstance(manifest.get("fragments"), list)
            or not manifest["fragments"]):
        raise ProjectionError("invalid bundled status material manifest")

    material = {
        "schema": "tap.internal-status-material/v1",
        "result_schema": manifest["result_schema"],
        "observations": {},
        "semantic_rules": [],
        "composition_rules": [],
        "presentation_rules": [],
        "document_root": None,
        "terminal_styles": {},
    }
    fragment_ids = set()
    rule_ids = set()
    roots = []
    allowed = {"schema", "id", "observations", "semantic_rules",
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
        overlap = set(material["observations"]) & set(observations)
        if overlap:
            raise ProjectionError(f"duplicate observation group: {sorted(overlap)[0]}")
        material["observations"].update(observations)

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
    return material


def observe(spec, snapshot):
    """Read one declared observation from a supplied snapshot carrier."""
    if (not isinstance(spec, dict) or set(spec) != {"path", "error"}
            or not isinstance(spec["path"], list) or not spec["path"]
            or not all(isinstance(part, str) and part for part in spec["path"])
            or not isinstance(spec["error"], str) or not spec["error"]):
        raise ProjectionError("invalid status observation source")

    errors = snapshot.get("inspection_errors", {})
    if spec["error"] in errors:
        return {"knowledge": "unknown", "reason": "inspection_failed",
                "message": errors[spec["error"]]}

    value = snapshot
    for part in spec["path"]:
        if not isinstance(value, dict) or part not in value:
            return {"knowledge": "unknown", "reason": "not_observed",
                    "message": "observation was not supplied"}
        value = value[part]
    return {"knowledge": "known", "value": value}


def public_status_result(snapshot, material=None):
    """Reduce the operational snapshot to the stable, compact public contract."""
    material = load_material() if material is None else material
    result = {"schema": material["result_schema"], "profile": snapshot.get("profile")}
    for group, declarations in material["observations"].items():
        result[group] = {item["name"]: observe(item["source"], snapshot)
                         for item in declarations}
    return result


def validate_status_result(result, material=None):
    material = load_material() if material is None else material
    if not isinstance(result, dict) or result.get("schema") != material["result_schema"]:
        raise ProjectionError("unsupported status result")
    if set(result) != {"schema", "profile", *material["observations"]}:
        raise ProjectionError("invalid status result fields")
    for group, declarations in material["observations"].items():
        expected = {item["name"] for item in declarations}
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


def _facts(result):
    claims = {}
    for group, observations in result.items():
        if group in ("schema", "profile"):
            continue
        for name, observation in observations.items():
            value = observation.get("value", observation.get("reason"))
            atom = Atom("observation", (group, name, observation["knowledge"], value))
            claims[atom] = Derivation("supplied-status-result", tuple())
    return claims


def project_status(result, material=None):
    """Run the two explicit chains: observations to meanings, meanings to slots."""
    material = load_material() if material is None else material
    validate_status_result(result, material)
    semantic = evaluate_rules(_facts(result), material["semantic_rules"])
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
