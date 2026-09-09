"""Shared observation and semantic-projection operations for command views."""
from dataclasses import dataclass

from .projection import (Atom, ProjectionError, document_from_claims, evaluate_rules,
                         select_candidates, validate_conservation)


@dataclass(frozen=True)
class MaterialProjection:
    meanings: tuple
    document: object
    provenance: dict


def observe_path(path, carrier):
    """Read a declared path without assigning domain meaning to absence."""
    value = carrier
    for part in path:
        if not isinstance(value, dict) or part not in value:
            return {"knowledge": "unknown", "reason": "not_observed",
                    "message": "observation was not supplied"}
        value = value[part]
    return {"knowledge": "known", "value": value}


def observe(spec, snapshot, errors=None):
    """Read one declared observation from a supplied snapshot carrier."""
    if (not isinstance(spec, dict) or set(spec) != {"path", "error"}
            or not isinstance(spec["path"], list) or not spec["path"]
            or not all(isinstance(part, str) and part for part in spec["path"])
            or not isinstance(spec["error"], str) or not spec["error"]):
        raise ProjectionError("invalid observation source")

    if not isinstance(snapshot, dict):
        raise ProjectionError("observation snapshot must be an object")
    errors = snapshot.get("inspection_errors", {}) if errors is None else errors
    if not isinstance(errors, dict):
        raise ProjectionError("observation errors must be an object")
    if spec["error"] in errors:
        return {"knowledge": "unknown", "reason": "inspection_failed",
                "message": errors[spec["error"]]}
    return observe_path(spec["path"], snapshot)


def project_claims(claims, material, supplied_operator):
    """Run the shared meanings -> selection -> composition -> document chain."""
    semantic = evaluate_rules(claims, material["semantic_rules"])
    section_selected = select_candidates(semantic)
    composed = evaluate_rules({**semantic, **section_selected},
                              material.get("composition_rules", []))
    selected = select_candidates(composed)
    meanings = tuple(sorted((atom for atom in selected if atom.relation == "meaning"),
                            key=Atom.identifier))
    interpreted = {atom: derivation for atom, derivation in composed.items()
                   if atom.relation != "meaning"}
    interpreted.update(selected)
    presented = evaluate_rules(interpreted, material["presentation_rules"])
    trace = {**composed, **selected, **presented}
    document = document_from_claims(trace, material["document_root"])
    validate_conservation(trace, meanings, document, supplied_operator)
    return MaterialProjection(meanings, document, trace)
