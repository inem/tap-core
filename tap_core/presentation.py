"""Executable #52 experiment: domain meaning becomes terminal notation in stages.

The first half derives claims about TAP from observations. The second half
decides what to communicate, verbalizes it, assigns visual roles, lays it out,
selects terminal notation and finally serializes terminal cells. Concrete
glyphs and punctuation exist only in the terminal plan.
"""


STATUS_OBSERVATIONS = "tap.status-observations/v1"
RUNTIME_ASSESSMENT = "tap.runtime-assessment/v1"
ROUTING_OBSERVATIONS = "tap.routing-observations/v1"
ROUTING_ASSESSMENT = "tap.routing-assessment/v1"
STATUS_MEANING = "tap.status-meaning/v1"
STATUS_MESSAGES = "tap.status-messages/v1"
VISUAL_DOCUMENT = "tap.visual-document/v1"
RENDER_DOCUMENT = "tap.render-document/v1"
TERMINAL_PLAN = "tap.terminal-plan/v1"


def _observed(snapshot, names):
    errors = snapshot.get("inspection_errors") or {}
    observations = {}
    for name in names:
        if name in errors:
            observations[name] = {
                "knowledge": "unknown",
                "reason": "inspection_failed",
                "message": str(errors[name]),
            }
        elif name not in snapshot:
            observations[name] = {
                "knowledge": "unknown",
                "reason": "not_observed",
            }
        else:
            observations[name] = {
                "knowledge": "known",
                "value": snapshot[name],
            }
    return observations


def _known(observations, name):
    item = observations[name]
    return item.get("knowledge") == "known", item.get("value")


def normalize_status_observations(snapshot):
    """Separate runtime facts from unavailable inspection results."""
    return {
        "schema": STATUS_OBSERVATIONS,
        "profile": snapshot.get("profile"),
        "port": snapshot.get("port"),
        "observations": _observed(
            snapshot, ("service_loaded", "pid", "port_owned", "port_open")),
    }


def assess_runtime(observed):
    """Derive one runtime claim while retaining the evidence behind it."""
    if observed.get("schema") != STATUS_OBSERVATIONS:
        raise ValueError("unsupported status observations")
    observations = observed["observations"]
    loaded_known, loaded = _known(observations, "service_loaded")
    pid_known, pid = _known(observations, "pid")
    open_known, port_open = _known(observations, "port_open")
    owned_known, port_owned = _known(observations, "port_owned")

    if not (loaded_known and pid_known and open_known):
        state, reason = "unknown", "inspection_incomplete"
    elif loaded is False and pid is None and port_open is False:
        state, reason = "stopped", "service_absent_and_port_closed"
    elif port_open is True and owned_known and port_owned is False:
        state, reason = "port_conflict", "listener_not_owned_by_profile"
    elif loaded is True and isinstance(pid, int) and pid > 0 and port_open is True:
        if not owned_known:
            state, reason = "unknown", "listener_ownership_unknown"
        elif port_owned is True:
            state, reason = "running", "service_owns_listener"
        else:
            state, reason = "port_conflict", "listener_not_owned_by_profile"
    elif loaded is True and isinstance(pid, int) and pid > 0 and port_open is False:
        state, reason = "broken", "service_has_no_listener"
    else:
        state, reason = "unknown", "inconsistent_runtime_observations"

    claim = {
        "subject": "tap.runtime",
        "predicate": "operational-state",
        "value": state,
        "reason": reason,
    }
    if state == "running":
        claim["process_id"] = pid
    if state in ("port_conflict", "broken") and observed.get("port") is not None:
        claim["port"] = observed["port"]
    return {
        "schema": RUNTIME_ASSESSMENT,
        "profile": observed.get("profile"),
        "claim": claim,
        "evidence": observations,
    }


def normalize_routing_observations(snapshot):
    """Separate routing configuration from observed system state."""
    return {
        "schema": ROUTING_OBSERVATIONS,
        "observations": _observed(
            snapshot, ("routing", "network_recovery_pending", "system_proxy_verified")),
    }


def assess_routing(observed):
    """Derive how browser and GUI-app traffic reaches this profile."""
    if observed.get("schema") != ROUTING_OBSERVATIONS:
        raise ValueError("unsupported routing observations")
    observations = observed["observations"]
    mode_known, mode = _known(observations, "routing")
    recovery_known, recovery = _known(observations, "network_recovery_pending")
    verified_known, verified = _known(observations, "system_proxy_verified")

    if not (mode_known and recovery_known and verified_known):
        state, reason = "unknown", "inspection_incomplete"
    elif mode == "explicit" and recovery is False and verified == "not_used":
        state, reason = "client_opt_in", "system_proxy_not_managed"
    elif mode == "explicit" and recovery is True:
        state, reason = "recovery_required", "unexpected_recovery_snapshot"
    elif mode == "system" and verified is True and recovery is True:
        state, reason = "capturing", "owned_system_proxy_verified"
    elif mode == "system" and verified is False and recovery is False:
        state, reason = "direct", "system_proxy_disabled"
    elif mode == "system" and verified is False and recovery is True:
        state, reason = "recovery_required", "owned_system_proxy_drifted"
    elif mode == "system" and verified is True and recovery is False:
        state, reason = "unowned_route", "recovery_snapshot_missing"
    else:
        state, reason = "unknown", "inconsistent_routing_observations"

    return {
        "schema": ROUTING_ASSESSMENT,
        "claim": {
            "subject": "browser-and-gui-traffic",
            "predicate": "route-to-profile",
            "value": state,
            "reason": reason,
        },
        "evidence": observations,
    }


def compose_status_meaning(runtime, routing):
    """Select claims, supporting meanings and actions worth communicating."""
    if runtime.get("schema") != RUNTIME_ASSESSMENT:
        raise ValueError("unsupported runtime assessment")
    if routing.get("schema") != ROUTING_ASSESSMENT:
        raise ValueError("unsupported routing assessment")

    runtime_claim = runtime["claim"]
    runtime_state = runtime_claim["value"]
    runtime_attention = {
        "running": "positive",
        "stopped": "negative",
        "port_conflict": "negative",
        "broken": "negative",
        "unknown": "unknown",
    }[runtime_state]
    runtime_item = {
        "subject": {"concept": "tap.runtime", "short-name": "tap"},
        "assertion": {
            "concept": "runtime-condition",
            "value": runtime_state,
            "attention": runtime_attention,
        },
        "attachments": [],
    }
    if runtime_state == "running":
        runtime_item["attachments"].append({
            "relation": "supporting-evidence",
            "concept": "process-id",
            "value": runtime_claim["process_id"],
        })
    elif runtime_state == "broken":
        runtime_item["attachments"].append({
            "relation": "explanation",
            "concept": "listener-state",
            "value": "missing",
        })
    elif runtime_state == "unknown":
        runtime_item["attachments"].append({
            "relation": "epistemic-limit",
            "concept": "inspection",
            "value": "incomplete",
        })

    routing_claim = routing["claim"]
    routing_state = routing_claim["value"]
    routing_attention = {
        "client_opt_in": "neutral",
        "capturing": "positive",
        "direct": "neutral",
        "recovery_required": "warning",
        "unowned_route": "warning",
        "unknown": "unknown",
    }[routing_state]
    routing_item = {
        "subject": {"concept": "browser-and-gui-traffic", "short-name": "browser/apps"},
        "assertion": {
            "concept": "traffic-route",
            "value": routing_state,
            "attention": routing_attention,
        },
        "attachments": [],
    }
    if routing_state == "client_opt_in":
        routing_item["attachments"].append({
            "relation": "participation-model",
            "concept": "client-configuration",
            "value": "opt-in",
        })
    elif routing_state == "capturing":
        routing_item["attachments"].append({
            "relation": "mechanism",
            "concept": "system-proxy",
            "value": "enabled-and-owned",
        })
    elif routing_state == "direct":
        routing_item["attachments"].append({
            "relation": "suggested-action",
            "concept": "command-invocation",
            "value": ["tap", "on"],
        })
    elif routing_state == "recovery_required":
        routing_item["attachments"].append({
            "relation": "suggested-action",
            "concept": "command-invocation",
            "value": ["tap", "off"],
        })
    elif routing_state == "unowned_route":
        routing_item["attachments"].append({
            "relation": "safety-condition",
            "concept": "recovery-snapshot",
            "value": "missing",
        })
    elif routing_state == "unknown":
        routing_item["attachments"].append({
            "relation": "epistemic-limit",
            "concept": "inspection",
            "value": "incomplete",
        })

    return {
        "schema": STATUS_MEANING,
        "purpose": "report-current-status",
        "items": [runtime_item, routing_item],
    }


def verbalize_status_meaning(meaning):
    """Turn domain concepts into words and discourse relations, without notation."""
    if meaning.get("schema") != STATUS_MEANING:
        raise ValueError("unsupported status meaning")
    runtime_words = {
        "running": "up",
        "stopped": "down",
        "port_conflict": "PORT STOLEN",
        "broken": "broken",
        "unknown": "unknown",
    }
    routing_words = {
        "client_opt_in": "explicit",
        "capturing": "capturing",
        "direct": "direct",
        "recovery_required": "routing drift",
        "unowned_route": "capturing",
        "unknown": "unknown",
    }

    def attachment_text(attachment):
        concept, value = attachment["concept"], attachment["value"]
        if concept == "process-id":
            return "PID " + str(value)
        if (concept, value) == ("listener-state", "missing"):
            return "service has no listener"
        if (concept, value) == ("inspection", "incomplete"):
            return "inspection incomplete"
        if (concept, value) == ("client-configuration", "opt-in"):
            return "clients opt in"
        if (concept, value) == ("system-proxy", "enabled-and-owned"):
            return "system proxy"
        if concept == "command-invocation":
            return " ".join(value)
        if (concept, value) == ("recovery-snapshot", "missing"):
            return "recovery snapshot missing"
        raise ValueError("unsupported status attachment")

    messages = []
    for item in meaning["items"]:
        concept = item["assertion"]["concept"]
        words = runtime_words if concept == "runtime-condition" else routing_words
        messages.append({
            "subject": item["subject"]["short-name"],
            "assertion": {
                "text": words[item["assertion"]["value"]],
                "attention": item["assertion"]["attention"],
            },
            "attachments": [
                {"relation": attachment["relation"], "text": attachment_text(attachment)}
                for attachment in item["attachments"]
            ],
        })
    return {"schema": STATUS_MESSAGES, "messages": messages}


def project_visual_semantics(messages):
    """Assign visible roles and hierarchy without selecting notation."""
    if messages.get("schema") != STATUS_MESSAGES:
        raise ValueError("unsupported status messages")
    indicator_forms = {
        "positive": "active-solid",
        "negative": "failure-cross",
        "neutral": "inactive-hollow",
        "warning": "warning-sign",
        "unknown": "unknown-sign",
    }
    items = []
    for message in messages["messages"]:
        attention = message["assertion"]["attention"]
        attachments = []
        for attachment in message["attachments"]:
            relation = attachment["relation"]
            attachments.append({
                "text": attachment["text"],
                "role": "secondary",
                "tone": "muted",
                "connection": "aside" if relation == "suggested-action" else "support",
                "enclosure": "parenthetical" if relation == "suggested-action" else "none",
            })
        items.append({
            "type": "status-item",
            "label": message["subject"],
            "indicator": {
                "role": "state",
                "form": indicator_forms[attention],
                "tone": attention,
            },
            "primary": {"text": message["assertion"]["text"], "role": "primary"},
            "attachments": attachments,
        })
    return {"schema": VISUAL_DOCUMENT, "group": "status-list", "items": items}


def layout_visual_document(visual):
    """Choose row grouping and alignment while preserving abstract visual roles."""
    if visual.get("schema") != VISUAL_DOCUMENT:
        raise ValueError("unsupported visual document")
    if visual.get("group") != "status-list":
        raise ValueError("unsupported visual group")
    return {
        "schema": RENDER_DOCUMENT,
        "layout": {"flow": "vertical", "label-column-width": 14},
        "blocks": [
            {
                "type": "status-row",
                "label": item["label"],
                "indicator": item["indicator"],
                "primary": item["primary"],
                "attachments": item["attachments"],
            }
            for item in visual["items"]
        ],
    }


def resolve_terminal_notation(document):
    """Map visual roles to terminal glyphs, separators and enclosures."""
    if document.get("schema") != RENDER_DOCUMENT:
        raise ValueError("unsupported render document")
    width = document.get("layout", {}).get("label-column-width", 0)
    if not isinstance(width, int) or width < 0:
        raise ValueError("invalid label width")
    indicator_notation = {
        "active-solid": {"text": "●", "ansi": "\033[32m"},
        "failure-cross": {"text": "✗", "ansi": "\033[31m"},
        "inactive-hollow": {"text": "○", "ansi": "\033[2m"},
        "warning-sign": {"text": "⚠", "ansi": "\033[33m"},
        "unknown-sign": {"text": "?", "ansi": "\033[33m"},
    }
    lines = []
    for block in document.get("blocks", []):
        if block.get("type") != "status-row":
            raise ValueError("unsupported render block")
        form = block.get("indicator", {}).get("form")
        if form not in indicator_notation:
            raise ValueError("unsupported indicator form")
        segments = [
            {"text": str(block.get("label", "")).ljust(width)},
            {**indicator_notation[form], "role": "indicator"},
            {"text": " " + str(block.get("primary", {}).get("text", ""))},
        ]
        for attachment in block.get("attachments", []):
            connection = attachment.get("connection")
            enclosure = attachment.get("enclosure")
            if connection == "support" and enclosure == "none":
                segments.append({
                    "text": " · " + str(attachment["text"]),
                    "ansi": "\033[2m",
                })
            elif connection == "aside" and enclosure == "parenthetical":
                segments.append({
                    "text": "   (" + str(attachment["text"]) + ")",
                    "ansi": "\033[2m",
                })
            else:
                raise ValueError("unsupported attachment notation")
        lines.append(segments)
    return {"schema": TERMINAL_PLAN, "lines": lines}


def serialize_terminal_plan(plan, color=False):
    """Serialize resolved terminal segments into the final text stream."""
    if plan.get("schema") != TERMINAL_PLAN:
        raise ValueError("unsupported terminal plan")
    lines = []
    for segments in plan.get("lines", []):
        line = ""
        for segment in segments:
            content = str(segment.get("text", ""))
            if color and segment.get("ansi"):
                content = segment["ansi"] + content + "\033[0m"
            line += content
        lines.append(line)
    return "\n".join(lines)


def status_artifacts(snapshot):
    """Expose every boundary so the experiment can inspect each transformation."""
    status_observations = normalize_status_observations(snapshot)
    runtime = assess_runtime(status_observations)
    routing_observations = normalize_routing_observations(snapshot)
    routing = assess_routing(routing_observations)
    meaning = compose_status_meaning(runtime, routing)
    messages = verbalize_status_meaning(meaning)
    visual = project_visual_semantics(messages)
    document = layout_visual_document(visual)
    terminal = resolve_terminal_notation(document)
    return {
        "status_observations": status_observations,
        "runtime_assessment": runtime,
        "routing_observations": routing_observations,
        "routing_assessment": routing,
        "meaning": meaning,
        "messages": messages,
        "visual": visual,
        "render_document": document,
        "terminal_plan": terminal,
    }


def status_terminal(snapshot, color=False):
    """Run the complete chain while keeping every stage independently inspectable."""
    plan = status_artifacts(snapshot)["terminal_plan"]
    return serialize_terminal_plan(plan, color=color)
