"""Invariant-centered experiment for projecting ``tap status`` to a terminal.

The domain model retains evidence and claim warrants. The semantic report owns
what is worth communicating. A surface renderer may change notation or omit
optional material, but every semantic token needs a report source and every
omission needs an explicit reason.
"""
from dataclasses import dataclass
from typing import Optional


STATUS_RESULT = "tap.status-result/v1"


@dataclass(frozen=True)
class Evidence:
    id: str
    knowledge: str
    value: object = None
    reason: Optional[str] = None
    message: Optional[str] = None


@dataclass(frozen=True)
class Claim:
    id: str
    subject: str
    predicate: str
    value: str
    reason: str
    warrants: tuple[str, ...]
    attributes: tuple[tuple[str, object], ...] = ()

    def attribute(self, name, default=None):
        return dict(self.attributes).get(name, default)


@dataclass(frozen=True)
class StatusModel:
    evidence: tuple[Evidence, ...]
    claims: tuple[Claim, ...]


@dataclass(frozen=True)
class Slot:
    id: str
    kind: str
    role: str
    sources: tuple[str, ...]
    value: Optional[str] = None
    optional: bool = False
    children: tuple["Slot", ...] = ()

    def walk(self):
        result = [self]
        for child in self.children:
            result.extend(child.walk())
        return tuple(result)


@dataclass(frozen=True)
class Omission:
    target: str
    reason: str


@dataclass(frozen=True)
class SemanticReport:
    root: Slot
    policy_ids: tuple[str, ...]
    claim_omissions: tuple[Omission, ...] = ()

    def slots(self):
        return self.root.walk()


@dataclass(frozen=True)
class SurfaceToken:
    text: str
    kind: str
    sources: tuple[str, ...] = ()
    style: Optional[str] = None
    layout_role: Optional[str] = None


@dataclass(frozen=True)
class SurfaceLine:
    source: str
    tokens: tuple[SurfaceToken, ...]


@dataclass(frozen=True)
class TerminalSurface:
    root_source: str
    lines: tuple[SurfaceLine, ...]
    width: int
    theme: str
    omissions: tuple[Omission, ...] = ()


@dataclass(frozen=True)
class ProjectionTrace:
    public_result: dict
    model: StatusModel
    report: SemanticReport
    surface: TerminalSurface


def _public_observation(snapshot, field):
    errors = snapshot.get("inspection_errors") or {}
    if field in errors:
        return {
            "knowledge": "unknown",
            "reason": "inspection_failed",
            "message": str(errors[field]),
        }
    if field not in snapshot:
        return {"knowledge": "unknown", "reason": "not_observed"}
    return {"knowledge": "known", "value": snapshot[field]}


def public_status_result(snapshot):
    """Select and version the status facts consumed by this projection."""
    return {
        "schema": STATUS_RESULT,
        "profile": snapshot.get("profile"),
        "runtime": {
            field: _public_observation(snapshot, field)
            for field in ("service_loaded", "pid", "port_owned", "port_open")
        },
        "routing": {
            field: _public_observation(snapshot, field)
            for field in ("routing", "network_recovery_pending", "system_proxy_verified")
        },
    }


def _evidence(group, observations):
    result = []
    for field, observation in observations.items():
        knowledge = observation.get("knowledge")
        if knowledge not in ("known", "unknown"):
            raise ValueError("invalid observation knowledge")
        if knowledge == "known" and "value" not in observation:
            raise ValueError("known observation has no value")
        result.append(Evidence(
            id=group + "." + field,
            knowledge=knowledge,
            value=observation.get("value"),
            reason=observation.get("reason"),
            message=observation.get("message"),
        ))
    return tuple(result)


def _value(evidence, id):
    item = next(item for item in evidence if item.id == id)
    return item.knowledge == "known", item.value


def interpret_status_result(result):
    """Derive runtime and routing claims from the versioned result."""
    if result.get("schema") != STATUS_RESULT:
        raise ValueError("unsupported status result")
    runtime_evidence = _evidence("runtime", result.get("runtime", {}))
    routing_evidence = _evidence("routing", result.get("routing", {}))
    evidence = runtime_evidence + routing_evidence

    loaded_known, loaded = _value(evidence, "runtime.service_loaded")
    pid_known, pid = _value(evidence, "runtime.pid")
    open_known, port_open = _value(evidence, "runtime.port_open")
    owned_known, port_owned = _value(evidence, "runtime.port_owned")
    if not (loaded_known and pid_known and open_known):
        runtime_state, runtime_reason = "unknown", "inspection_incomplete"
    elif loaded is False and pid is None and port_open is False:
        runtime_state, runtime_reason = "stopped", "service_absent_and_port_closed"
    elif port_open is True and owned_known and port_owned is False:
        runtime_state, runtime_reason = "port_conflict", "listener_not_owned_by_profile"
    elif loaded is True and isinstance(pid, int) and pid > 0 and port_open is True:
        if not owned_known:
            runtime_state, runtime_reason = "unknown", "listener_ownership_unknown"
        elif port_owned is True:
            runtime_state, runtime_reason = "running", "service_owns_listener"
        else:
            runtime_state, runtime_reason = "port_conflict", "listener_not_owned_by_profile"
    elif loaded is True and isinstance(pid, int) and pid > 0 and port_open is False:
        runtime_state, runtime_reason = "broken", "service_has_no_listener"
    else:
        runtime_state, runtime_reason = "unknown", "inconsistent_runtime_observations"

    runtime_attributes = (("pid", pid),) if runtime_state == "running" else ()
    runtime_claim = Claim(
        id="claim.runtime",
        subject="tap.runtime",
        predicate="operational-state",
        value=runtime_state,
        reason=runtime_reason,
        warrants=tuple(item.id for item in runtime_evidence),
        attributes=runtime_attributes,
    )

    mode_known, mode = _value(evidence, "routing.routing")
    recovery_known, recovery = _value(evidence, "routing.network_recovery_pending")
    verified_known, verified = _value(evidence, "routing.system_proxy_verified")
    if not (mode_known and recovery_known and verified_known):
        routing_state, routing_reason = "unknown", "inspection_incomplete"
    elif mode == "explicit" and recovery is False and verified == "not_used":
        routing_state, routing_reason = "client_opt_in", "system_proxy_not_managed"
    elif mode == "explicit" and recovery is True:
        routing_state, routing_reason = "recovery_required", "unexpected_recovery_snapshot"
    elif mode == "system" and verified is True and recovery is True:
        routing_state, routing_reason = "capturing", "owned_system_proxy_verified"
    elif mode == "system" and verified is False and recovery is False:
        routing_state, routing_reason = "direct", "system_proxy_disabled"
    elif mode == "system" and verified is False and recovery is True:
        routing_state, routing_reason = "recovery_required", "owned_system_proxy_drifted"
    elif mode == "system" and verified is True and recovery is False:
        routing_state, routing_reason = "unowned_route", "recovery_snapshot_missing"
    else:
        routing_state, routing_reason = "unknown", "inconsistent_routing_observations"

    routing_claim = Claim(
        id="claim.routing",
        subject="browser-and-gui-traffic",
        predicate="route-to-profile",
        value=routing_state,
        reason=routing_reason,
        warrants=tuple(item.id for item in routing_evidence),
    )
    model = StatusModel(evidence=evidence, claims=(runtime_claim, routing_claim))
    validate_model(model)
    return model


def _leaf(id, role, value, source):
    return Slot(id=id, kind="leaf", role=role, value=value, sources=(source,))


def _attachment(id, relation, content_role, content, source):
    return Slot(
        id=id,
        kind="group",
        role=relation,
        value=content_role,
        sources=(source,),
        optional=True,
        children=(_leaf(id + ".content", content_role, content, source),),
    )


def project_compact_status(model):
    """Choose the two compact messages without choosing terminal notation."""
    validate_model(model)
    claims = {claim.id: claim for claim in model.claims}
    runtime = claims["claim.runtime"]
    routing = claims["claim.routing"]

    runtime_words = {
        "running": ("positive-active", "up"),
        "stopped": ("negative", "down"),
        "port_conflict": ("negative", "PORT STOLEN"),
        "broken": ("negative", "broken"),
        "unknown": ("unknown", "unknown"),
    }
    indicator, word = runtime_words[runtime.value]
    runtime_attachments = ()
    if runtime.value == "running":
        runtime_attachments = (_attachment(
            "runtime.pid", "support", "evidence", "PID " + str(runtime.attribute("pid")),
            "runtime.pid"),)
    elif runtime.value == "broken":
        runtime_attachments = (_attachment(
            "runtime.problem", "support", "explanation", "service has no listener",
            runtime.id),)
    elif runtime.value == "unknown":
        runtime_attachments = (_attachment(
            "runtime.unknown", "support", "epistemic-limit", "inspection incomplete",
            runtime.id),)
    runtime_line = Slot(
        id="line.runtime",
        kind="group",
        role="status-item",
        sources=(runtime.id,),
        children=(
            _leaf("runtime.subject", "subject", "tap", runtime.id),
            _leaf("runtime.indicator", "state-indicator", indicator, runtime.id),
            _leaf("runtime.assertion", "assertion", word, runtime.id),
            *runtime_attachments,
        ),
    )

    routing_words = {
        "client_opt_in": ("neutral-inactive", "explicit"),
        "capturing": ("positive-active", "capturing"),
        "direct": ("neutral-inactive", "direct"),
        "recovery_required": ("warning", "routing drift"),
        "unowned_route": ("warning", "capturing"),
        "unknown": ("unknown", "unknown"),
    }
    indicator, word = routing_words[routing.value]
    relation, content, content_role = {
        "client_opt_in": ("support", "clients opt in", "participation-model"),
        "capturing": ("support", "system proxy", "mechanism"),
        "direct": ("aside", "tap on", "suggested-action"),
        "recovery_required": ("aside", "tap off", "suggested-action"),
        "unowned_route": ("support", "recovery snapshot missing", "safety-condition"),
        "unknown": ("support", "inspection incomplete", "epistemic-limit"),
    }[routing.value]
    routing_line = Slot(
        id="line.routing",
        kind="group",
        role="status-item",
        sources=(routing.id,),
        children=(
            _leaf("routing.subject", "subject", "browser/apps", routing.id),
            _leaf("routing.indicator", "state-indicator", indicator, routing.id),
            _leaf("routing.assertion", "assertion", word, routing.id),
            _attachment("routing.attachment", relation, content_role, content, routing.id),
        ),
    )
    policy = "policy.compact-status/v1"
    report = SemanticReport(
        root=Slot(
            id="report.status",
            kind="group",
            role="status-list",
            sources=(policy,),
            children=(runtime_line, routing_line),
        ),
        policy_ids=(policy,),
    )
    validate_report(model, report)
    return report


def _semantic(text, source, style=None):
    return SurfaceToken(text=text, kind="semantic", sources=(source,), style=style)


def _layout(text, role):
    return SurfaceToken(text=text, kind="layout", layout_role=role)


def _token_width(tokens):
    return sum(len(token.text) for token in tokens)


def _child(slot, role):
    matches = [child for child in slot.children if child.role == role]
    if len(matches) != 1:
        raise ValueError("status item needs exactly one " + role + " slot")
    return matches[0]


def render_terminal(report, width=80, theme="unicode"):
    """Render report meanings and record every width-driven omission."""
    if not isinstance(width, int) or width < 1:
        raise ValueError("terminal width must be a positive integer")
    notation = {
        "unicode": {
            "positive-active": "●", "negative": "✗", "neutral-inactive": "○",
            "warning": "⚠", "unknown": "?", "support": "·", "open": "(", "close": ")",
        },
        "ascii": {
            "positive-active": "+", "negative": "x", "neutral-inactive": "o",
            "warning": "!", "unknown": "?", "support": "-", "open": "[", "close": "]",
        },
    }
    if theme not in notation:
        raise ValueError("unsupported terminal theme")
    glyphs = notation[theme]
    styles = {
        "positive-active": "positive", "negative": "negative",
        "neutral-inactive": "muted", "warning": "warning", "unknown": "warning",
    }
    lines = []
    omissions = []
    for line in report.root.children:
        subject = _child(line, "subject")
        indicator = _child(line, "state-indicator")
        assertion = _child(line, "assertion")
        tokens = [
            _semantic(subject.value, subject.id),
            _layout(" " * max(0, 14 - len(subject.value)), "label-column-padding"),
            _semantic(glyphs[indicator.value], indicator.id, styles[indicator.value]),
            _layout(" ", "indicator-value-gap"),
            _semantic(assertion.value, assertion.id),
        ]
        attachments = [child for child in line.children if child.role in ("support", "aside")]
        for attachment in attachments:
            content = attachment.children[0]
            if attachment.role == "support":
                addition = [
                    _layout(" ", "support-leading-gap"),
                    _semantic(glyphs["support"], attachment.id, "muted"),
                    _layout(" ", "support-content-gap"),
                    _semantic(content.value, content.id, "muted"),
                ]
            elif attachment.role == "aside":
                addition = [
                    _layout("   ", "aside-leading-gap"),
                    _semantic(glyphs["open"], attachment.id, "muted"),
                    _semantic(content.value, content.id, "muted"),
                    _semantic(glyphs["close"], attachment.id, "muted"),
                ]
            else:
                raise ValueError("unsupported attachment relation")
            if _token_width(tokens + addition) <= width:
                tokens.extend(addition)
            elif attachment.optional:
                omissions.append(Omission(
                    target=attachment.id,
                    reason="terminal-width:" + str(width),
                ))
            else:
                raise ValueError("required status meaning does not fit terminal width")
        lines.append(SurfaceLine(source=line.id, tokens=tuple(tokens)))
    return TerminalSurface(root_source=report.root.id, lines=tuple(lines), width=width, theme=theme,
                           omissions=tuple(omissions))


def serialize_terminal(surface, color=False):
    """Emit text and optional ANSI styling from an already validated surface."""
    ansi = {"positive": "\033[32m", "negative": "\033[31m",
            "warning": "\033[33m", "muted": "\033[2m"}
    output = []
    for line in surface.lines:
        rendered = ""
        for token in line.tokens:
            content = token.text
            if color and token.style:
                content = ansi[token.style] + content + "\033[0m"
            rendered += content
        output.append(rendered)
    return "\n".join(output)


def validate_model(model):
    """Require every claim to be warranted by retained evidence."""
    evidence_ids = {item.id for item in model.evidence}
    claim_ids = {claim.id for claim in model.claims}
    if len(evidence_ids) != len(model.evidence) or len(claim_ids) != len(model.claims):
        raise ValueError("duplicate model identifier")
    warranted = set()
    for claim in model.claims:
        if not claim.warrants or not set(claim.warrants) <= evidence_ids:
            raise ValueError("claim has missing evidence warrant")
        warranted.update(claim.warrants)
    if warranted != evidence_ids:
        raise ValueError("evidence distinction was silently dropped")


def validate_report(model, report):
    """Require every nested slot to be grounded and structurally valid."""
    model_ids = ({item.id for item in model.evidence}
                 | {claim.id for claim in model.claims}
                 | set(report.policy_ids))
    slots = report.slots()
    slot_ids = {slot.id for slot in slots}
    if len(slot_ids) != len(slots):
        raise ValueError("duplicate report slot identifier")
    claim_coverage = set()
    for slot in slots:
        if not slot.sources or not set(slot.sources) <= model_ids:
            raise ValueError("report slot has no valid source")
        if slot.kind == "leaf" and slot.children:
            raise ValueError("leaf slot cannot contain children")
        if slot.kind == "group" and not slot.children:
            raise ValueError("group slot must contain children")
        claim_coverage.update(source for source in slot.sources if source.startswith("claim."))
    if report.root.role != "status-list":
        raise ValueError("report root must be a status-list slot")
    if any(line.kind != "group" or line.role != "status-item"
           for line in report.root.children):
        raise ValueError("status-list children must be status-item slots")
    for line in report.root.children:
        for role in ("subject", "state-indicator", "assertion"):
            _child(line, role)
        for attachment in line.children[3:]:
            if (attachment.kind != "group" or attachment.role not in ("support", "aside")
                    or len(attachment.children) != 1
                    or attachment.children[0].kind != "leaf"):
                raise ValueError("invalid nested attachment slot")
    omitted = set()
    for omission in report.claim_omissions:
        if not omission.reason:
            raise ValueError("claim omission has no reason")
        omitted.add(omission.target)
    claim_ids = {claim.id for claim in model.claims}
    if claim_coverage & omitted:
        raise ValueError("claim is both projected and omitted")
    if claim_coverage | omitted != claim_ids:
        raise ValueError("claim distinction was silently dropped")


def validate_surface(report, surface):
    """Enforce traceable tokens and explicit omission of optional slot subtrees."""
    slots = report.slots()
    slot_by_id = {slot.id: slot for slot in slots}
    report_line_ids = [line.id for line in report.root.children]
    surface_line_ids = [line.source for line in surface.lines]
    if surface.root_source != report.root.id or surface_line_ids != report_line_ids:
        raise ValueError("report line structure was not preserved")
    covered = {report.root.id, *surface_line_ids}
    for line in surface.lines:
        for token in line.tokens:
            if token.kind == "semantic":
                if not token.sources or not set(token.sources) <= set(slot_by_id):
                    raise ValueError("semantic surface token has no valid source")
                if token.layout_role:
                    raise ValueError("semantic token cannot claim a layout role")
                covered.update(token.sources)
            elif token.kind == "layout":
                if token.sources or not token.layout_role:
                    raise ValueError("layout token needs only a declared layout role")
            else:
                raise ValueError("unknown surface token kind")
    omitted = set()
    for omission in surface.omissions:
        target = slot_by_id.get(omission.target)
        if not omission.reason or target is None:
            raise ValueError("invalid surface omission")
        if not target.optional:
            raise ValueError("required meaning was omitted")
        omitted.update(slot.id for slot in target.walk())
    if covered & omitted:
        raise ValueError("meaning is both rendered and omitted")
    if covered | omitted != set(slot_by_id):
        raise ValueError("report meaning was silently dropped")


def validate_projection(trace):
    """Check the conservation invariant across the complete projection trace."""
    validate_model(trace.model)
    validate_report(trace.model, trace.report)
    validate_surface(trace.report, trace.surface)


def project_status(snapshot, width=80, theme="unicode"):
    """Return an inspectable, validated trace for one status projection."""
    result = public_status_result(snapshot)
    model = interpret_status_result(result)
    report = project_compact_status(model)
    surface = render_terminal(report, width=width, theme=theme)
    trace = ProjectionTrace(result, model, report, surface)
    validate_projection(trace)
    return trace


def status_terminal(snapshot, width=80, theme="unicode", color=False):
    """Produce terminal text only after the complete trace satisfies the invariant."""
    return serialize_terminal(project_status(snapshot, width, theme).surface, color=color)
