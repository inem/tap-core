"""Small data-driven projection kernel used by human-facing command output."""
from dataclasses import dataclass
import json
from typing import Optional


class ProjectionError(ValueError):
    pass


class ProjectionConflict(ProjectionError):
    def __init__(self, key, candidates):
        super().__init__(f"incompatible candidates for {key}: {candidates}")
        self.key = key
        self.candidates = candidates


def _freeze(value):
    if isinstance(value, dict):
        return tuple(sorted((key, _freeze(item)) for key, item in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value):
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


@dataclass(frozen=True, order=True)
class Atom:
    relation: str
    arguments: tuple

    @classmethod
    def from_value(cls, value):
        if not isinstance(value, (list, tuple)) or not value or not isinstance(value[0], str):
            raise ProjectionError(f"invalid atom: {value!r}")
        return cls(value[0], tuple(_freeze(item) for item in value[1:]))

    def value(self):
        return [self.relation, *(_thaw(item) for item in self.arguments)]

    def identifier(self):
        return json.dumps(self.value(), ensure_ascii=False, separators=(",", ":"))


@dataclass(frozen=True)
class Derivation:
    operator: str
    warrants: tuple


@dataclass(frozen=True)
class Slot:
    identifier: str
    parent: Optional[str]
    order: int
    kind: str
    role: str
    optional: bool
    text: str
    style: Optional[str]
    sources: tuple


@dataclass(frozen=True)
class Document:
    root: str
    slots: tuple
    source_ids: frozenset


@dataclass(frozen=True)
class RenderResult:
    text: str
    omissions: tuple


def _match(pattern, value, bindings):
    if isinstance(pattern, dict):
        variable = pattern.get("var")
        test = pattern.get("test")
        if set(pattern) != {"var", "test"} or not isinstance(variable, str):
            raise ProjectionError(f"invalid matcher: {pattern!r}")
        if test == "positive-int" and (type(value) is not int or value <= 0):
            return None
        if test not in ("positive-int",):
            raise ProjectionError(f"unknown matcher test: {test}")
        return _match(variable, value, bindings)
    if isinstance(pattern, str) and pattern.startswith("$"):
        if pattern == "$_":
            return bindings
        if pattern in bindings and bindings[pattern] != value:
            return None
        return {**bindings, pattern: value}
    if isinstance(pattern, (list, tuple)):
        if not isinstance(value, tuple) or len(pattern) != len(value):
            return None
        result = bindings
        for expected, actual in zip(pattern, value):
            result = _match(expected, actual, result)
            if result is None:
                return None
        return result
    return bindings if pattern == value else None


def _atom_match(pattern, atom, bindings):
    if not pattern or pattern[0] != atom.relation or len(pattern) - 1 != len(atom.arguments):
        return None
    result = bindings
    for expected, actual in zip(pattern[1:], atom.arguments):
        result = _match(expected, actual, result)
        if result is None:
            return None
    return result


def _substitute(value, bindings):
    if isinstance(value, str) and value.startswith("$"):
        if value not in bindings:
            raise ProjectionError(f"unbound variable: {value}")
        return bindings[value]
    if isinstance(value, dict):
        if set(value) != {"format", "args"}:
            raise ProjectionError(f"invalid template: {value!r}")
        arguments = [_substitute(item, bindings) for item in value["args"]]
        return value["format"].format(*arguments)
    if isinstance(value, list):
        return tuple(_freeze(_substitute(item, bindings)) for item in value)
    return value


def evaluate_rules(claims, rules, limit=32):
    """Close finite declarative rules over atoms, retaining direct provenance."""
    result = dict(claims)
    ordered_rules = sorted(rules, key=lambda rule: rule["id"])
    for _ in range(limit):
        changed = False
        available = sorted(result, key=Atom.identifier)
        for rule in ordered_rules:
            matches = [({}, tuple())]
            for pattern in rule.get("when", []):
                next_matches = []
                for bindings, warrants in matches:
                    for atom in available:
                        updated = _atom_match(pattern, atom, bindings)
                        if updated is not None:
                            next_matches.append((updated, warrants + (atom,)))
                matches = next_matches
            for bindings, warrants in matches:
                for template in rule.get("emit", []):
                    atom = Atom.from_value([template[0], *(_substitute(item, bindings) for item in template[1:])])
                    if atom not in result:
                        result[atom] = Derivation(rule["id"], warrants)
                        changed = True
        if not changed:
            return result
    raise ProjectionError(f"rule closure exceeded {limit} rounds")


def select_candidates(claims):
    """Select max-rank candidates; equal-rank incompatible values are errors."""
    groups = {}
    for atom in claims:
        if atom.relation == "candidate":
            if len(atom.arguments) != 6 or type(atom.arguments[3]) is not int:
                raise ProjectionError(f"invalid candidate: {atom.value()!r}")
            groups.setdefault(atom.arguments[:2], []).append(atom)
    selected = {}
    for key, candidates in sorted(groups.items()):
        maximum = max(candidate.arguments[3] for candidate in candidates)
        winners = sorted((candidate for candidate in candidates if candidate.arguments[3] == maximum),
                         key=Atom.identifier)
        payloads = {candidate.arguments[2:3] + candidate.arguments[4:] for candidate in winners}
        if len(payloads) != 1:
            raise ProjectionConflict(key, tuple(candidate.value() for candidate in winners))
        subject, predicate, value, _, reason, attributes = winners[0].arguments
        meaning = Atom("meaning", (subject, predicate, value, reason, attributes))
        derivation = Derivation("select:max-rank", tuple(winners))
        selected[meaning] = derivation
        for name, item in attributes:
            selected[Atom("attribute", (subject, name, item))] = derivation
    return selected


def document_from_claims(claims, root):
    slots = []
    for atom in claims:
        if atom.relation != "slot":
            continue
        if len(atom.arguments) != 9:
            raise ProjectionError(f"invalid slot: {atom.value()!r}")
        identifier, parent, order, kind, role, optional, text, style, sources = atom.arguments
        if not (isinstance(identifier, str) and (parent is None or isinstance(parent, str))
                and type(order) is int and kind in ("group", "leaf")
                and isinstance(role, str) and type(optional) is bool and isinstance(text, str)
                and (style is None or isinstance(style, str)) and isinstance(sources, tuple)):
            raise ProjectionError(f"invalid slot fields: {atom.value()!r}")
        source_ids = tuple(Atom.from_value(item).identifier() for item in sources)
        slots.append(Slot(identifier, parent, order, kind, role, optional, text, style, source_ids))
    document = Document(root, tuple(slots), frozenset(atom.identifier() for atom in claims))
    validate_document(document)
    return document


def validate_document(document):
    by_id = {}
    for slot in document.slots:
        if slot.identifier in by_id:
            raise ProjectionError(f"duplicate slot id: {slot.identifier}")
        by_id[slot.identifier] = slot
    if document.root not in by_id or by_id[document.root].parent is not None:
        raise ProjectionError("document root is missing or has a parent")
    sibling_orders = set()
    for slot in document.slots:
        if slot.identifier != document.root and slot.parent not in by_id:
            raise ProjectionError(f"missing parent for slot: {slot.identifier}")
        sibling = (slot.parent, slot.order)
        if sibling in sibling_orders:
            raise ProjectionError(f"duplicate sibling order under {slot.parent}: {slot.order}")
        sibling_orders.add(sibling)
        if any(source not in document.source_ids for source in slot.sources):
            raise ProjectionError(f"ungrounded slot: {slot.identifier}")
        visited = set()
        cursor = slot
        while cursor.identifier != document.root:
            if cursor.identifier in visited:
                raise ProjectionError(f"slot parent cycle: {slot.identifier}")
            visited.add(cursor.identifier)
            cursor = by_id[cursor.parent]
    children = {identifier: [] for identifier in by_id}
    for slot in document.slots:
        if slot.parent is not None:
            children[slot.parent].append(slot)
    for slot in document.slots:
        if slot.kind == "leaf" and children[slot.identifier]:
            raise ProjectionError(f"leaf slot has children: {slot.identifier}")
        if slot.kind == "group" and slot.text:
            raise ProjectionError(f"group slot contains text: {slot.identifier}")
        if slot.optional:
            parent = by_id.get(slot.parent)
            if parent is None or parent.parent != document.root:
                raise ProjectionError(f"optional slot is outside a direct line group: {slot.identifier}")
    return children


def render_terminal_result(document, width=80, color=False, styles=None):
    if type(width) is not int or width < 1:
        raise ProjectionError("width must be a positive integer")
    children = validate_document(document)
    for values in children.values():
        values.sort(key=lambda slot: slot.order)

    def plain(slot):
        return slot.text if slot.kind == "leaf" else "".join(plain(child) for child in children[slot.identifier])

    def styled(slot):
        if slot.kind == "group":
            return "".join(styled(child) for child in children[slot.identifier])
        if color and slot.style:
            if not isinstance(styles, dict) or slot.style not in styles:
                raise ProjectionError(f"missing terminal style: {slot.style}")
            return f"{styles[slot.style]}{slot.text}\x1b[0m"
        return slot.text

    lines = []
    omissions = []
    root_children = children[document.root]
    if any(slot.kind != "group" for slot in root_children):
        raise ProjectionError("root children must be line groups")
    for line in root_children:
        included = []
        used = 0
        for child in children[line.identifier]:
            amount = len(plain(child))
            if used + amount > width and child.optional:
                omissions.append({"slot": child.identifier, "reason": "does-not-fit",
                                  "width": width, "required_width": used + amount})
                continue
            if used + amount > width:
                raise ProjectionError(f"required content exceeds width {width}: {line.identifier}")
            included.append(child)
            used += amount
        lines.append("".join(styled(child) for child in included))
    return RenderResult("\n".join(lines), tuple(omissions))


def render_terminal(document, width=80, color=False, styles=None):
    return render_terminal_result(document, width, color, styles).text
