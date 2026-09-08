"""Versioned status result and its optional human presentation."""
from dataclasses import dataclass
import json
from pathlib import Path

from .projection import (Atom, Derivation, ProjectionError, document_from_claims,
                         evaluate_rules, render_terminal, select_candidates,
                         validate_conservation)


MATERIAL = Path(__file__).with_name("data") / "status_projection_v1.json"


@dataclass(frozen=True)
class StatusProjection:
    result: dict
    meanings: tuple
    document: object
    provenance: dict


def load_material(path=MATERIAL):
    value = json.loads(path.read_text(encoding="utf-8"))
    required = {"schema", "result_schema", "observations", "semantic_rules",
                "presentation_rules", "document_root", "terminal_styles"}
    if not isinstance(value, dict) or set(value) != required:
        raise ProjectionError("invalid bundled status material")
    if value["schema"] != "tap.internal-status-material/v1":
        raise ProjectionError("unsupported bundled status material")
    return value


def _observation(snapshot, raw_name):
    errors = snapshot.get("inspection_errors", {})
    if raw_name in errors:
        return {"knowledge": "unknown", "reason": "inspection_failed", "message": errors[raw_name]}
    if raw_name not in snapshot:
        return {"knowledge": "unknown", "reason": "not_observed",
                "message": "observation was not supplied"}
    return {"knowledge": "known", "value": snapshot[raw_name]}


def public_status_result(snapshot, material=None):
    """Reduce the operational snapshot to the stable, compact public contract."""
    material = load_material() if material is None else material
    result = {"schema": material["result_schema"], "profile": snapshot.get("profile")}
    for group, declarations in material["observations"].items():
        result[group] = {item["name"]: _observation(snapshot, item["raw"])
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
    selected = select_candidates(semantic)
    meanings = tuple(sorted((atom for atom in selected if atom.relation == "meaning"),
                            key=Atom.identifier))
    interpreted = {**semantic, **selected}
    presented = evaluate_rules(interpreted, material["presentation_rules"])
    document = document_from_claims(presented, material["document_root"])
    validate_conservation(presented, meanings, document, "supplied-status-result")
    return StatusProjection(result, meanings, document, presented)


def terminal_status(result, width=80, color=False, material=None):
    material = load_material() if material is None else material
    return render_terminal(project_status(result, material).document, width, color,
                           material["terminal_styles"])
