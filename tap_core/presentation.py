"""First #52 semantic-to-terminal slices for ``tap status`` rows.

Each function owns one transformation and is deliberately pure.  The terminal
renderer only knows the render-document vocabulary; TAP-specific conclusions
are complete before lowering reaches it.
"""


STATUS_OBSERVATIONS = "tap.status-observations/v1"
STATUS_SUMMARY = "tap.status-summary/v1"
ROUTING_OBSERVATIONS = "tap.routing-observations/v1"
ROUTING_SUMMARY = "tap.routing-summary/v1"
STATUS_LINE = "tap.status-line/v1"
RENDER_DOCUMENT = "tap.render-document/v1"


def normalize_status_observations(snapshot):
    """Separate known values from inspection failures in today's status JSON."""
    errors = snapshot.get("inspection_errors") or {}
    observations = {}
    for name in ("service_loaded", "pid", "port_owned", "port_open"):
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
    return {
        "schema": STATUS_OBSERVATIONS,
        "profile": snapshot.get("profile"),
        "port": snapshot.get("port"),
        "observations": observations,
    }


def assess_status_summary(normalized):
    """Derive one truthful runtime claim and retain its evidence."""
    if normalized.get("schema") != STATUS_OBSERVATIONS:
        raise ValueError("unsupported status observations")
    observations = normalized["observations"]

    def known(name):
        item = observations[name]
        return item.get("knowledge") == "known", item.get("value")

    loaded_known, loaded = known("service_loaded")
    pid_known, pid = known("pid")
    open_known, port_open = known("port_open")
    owned_known, port_owned = known("port_owned")

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
        else:  # Kept explicit if the conditions above are changed later.
            state, reason = "port_conflict", "listener_not_owned_by_profile"
    elif loaded is True and isinstance(pid, int) and pid > 0 and port_open is False:
        state, reason = "broken", "service_has_no_listener"
    else:
        state, reason = "unknown", "inconsistent_runtime_observations"

    runtime = {"state": state, "reason": reason}
    if state == "running":
        runtime["pid"] = pid
    if state in ("port_conflict", "broken") and normalized.get("port") is not None:
        runtime["port"] = normalized["port"]
    return {
        "schema": STATUS_SUMMARY,
        "profile": normalized.get("profile"),
        "runtime": runtime,
        "evidence": observations,
    }


def project_status_line(summary):
    """Choose human wording for the one status line; no terminal layout yet."""
    if summary.get("schema") != STATUS_SUMMARY:
        raise ValueError("unsupported status summary")
    runtime = summary["runtime"]
    state = runtime["state"]
    if state == "running":
        row = {"mark": "success", "label": "tap", "value": "up",
               "detail": "PID " + str(runtime["pid"])}
    elif state == "stopped":
        row = {"mark": "error", "label": "tap", "value": "down"}
    elif state == "port_conflict":
        row = {"mark": "error", "label": "tap", "value": "PORT STOLEN"}
    elif state == "broken":
        row = {"mark": "error", "label": "tap", "value": "broken",
               "detail": "service has no listener"}
    else:
        row = {"mark": "unknown", "label": "tap", "value": "unknown",
               "detail": "inspection incomplete"}
    return {"schema": STATUS_LINE, "row": row}


def normalize_routing_observations(snapshot):
    """Separate routing configuration from observed system state."""
    errors = snapshot.get("inspection_errors") or {}
    observations = {}
    for name in ("routing", "network_recovery_pending", "system_proxy_verified"):
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
    return {
        "schema": ROUTING_OBSERVATIONS,
        "observations": observations,
    }


def assess_routing_summary(normalized):
    """Derive how browser and GUI-app traffic reaches this profile."""
    if normalized.get("schema") != ROUTING_OBSERVATIONS:
        raise ValueError("unsupported routing observations")
    observations = normalized["observations"]

    def known(name):
        item = observations[name]
        return item.get("knowledge") == "known", item.get("value")

    mode_known, mode = known("routing")
    recovery_known, recovery = known("network_recovery_pending")
    verified_known, verified = known("system_proxy_verified")

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
        "schema": ROUTING_SUMMARY,
        "traffic": {"state": state, "reason": reason},
        "evidence": observations,
    }


def project_routing_line(summary):
    """Choose terminal-neutral content for the browser/apps status row."""
    if summary.get("schema") != ROUTING_SUMMARY:
        raise ValueError("unsupported routing summary")
    traffic = summary["traffic"]
    state = traffic["state"]
    if state == "client_opt_in":
        row = {"mark": "neutral", "label": "browser/apps", "value": "explicit",
               "detail": "clients opt in"}
    elif state == "capturing":
        row = {"mark": "success", "label": "browser/apps", "value": "capturing",
               "detail": "system proxy"}
    elif state == "direct":
        row = {"mark": "neutral", "label": "browser/apps", "value": "direct",
               "hint": "tap on"}
    elif state == "recovery_required":
        row = {"mark": "warning", "label": "browser/apps", "value": "routing drift",
               "hint": "tap off"}
    elif state == "unowned_route":
        row = {"mark": "warning", "label": "browser/apps", "value": "capturing",
               "detail": "recovery snapshot missing"}
    else:
        row = {"mark": "unknown", "label": "browser/apps", "value": "unknown",
               "detail": "inspection incomplete"}
    return {"schema": STATUS_LINE, "row": row}


def lower_status_lines(views):
    """Combine TAP-specific rows into one renderer-neutral document."""
    if any(view.get("schema") != STATUS_LINE for view in views):
        raise ValueError("unsupported status line")
    return {
        "schema": RENDER_DOCUMENT,
        "layout": {"label_width": 14},
        "blocks": [{"type": "status_row", **view["row"]} for view in views],
    }


def lower_status_line(view):
    """Backward-compatible convenience for lowering one status row."""
    return lower_status_lines([view])


def render_terminal(document, color=False):
    """Render generic blocks.  This function contains no TAP field or state."""
    if document.get("schema") != RENDER_DOCUMENT:
        raise ValueError("unsupported render document")
    width = document.get("layout", {}).get("label_width", 0)
    if not isinstance(width, int) or width < 0:
        raise ValueError("invalid label width")
    marks = {
        "success": ("●", "\033[32m"),
        "error": ("✗", "\033[31m"),
        "unknown": ("?", "\033[33m"),
        "warning": ("⚠", "\033[33m"),
        "neutral": ("○", "\033[2m"),
    }
    lines = []
    for block in document.get("blocks", []):
        if block.get("type") != "status_row":
            raise ValueError("unsupported render block")
        if block.get("mark") not in marks:
            raise ValueError("unsupported status mark")
        glyph, ansi = marks[block["mark"]]
        mark = ansi + glyph + "\033[0m" if color else glyph
        line = str(block.get("label", "")).ljust(width) + mark + " " + str(block.get("value", ""))
        if block.get("detail"):
            line += " · " + str(block["detail"])
        if block.get("hint"):
            line += "   (" + str(block["hint"]) + ")"
        lines.append(line)
    return "\n".join(lines)


def status_terminal(snapshot, color=False):
    """Run both independent semantic branches and render their shared document."""
    observations = normalize_status_observations(snapshot)
    summary = assess_status_summary(observations)
    runtime_view = project_status_line(summary)
    routing_observations = normalize_routing_observations(snapshot)
    routing_summary = assess_routing_summary(routing_observations)
    routing_view = project_routing_line(routing_summary)
    document = lower_status_lines([runtime_view, routing_view])
    return render_terminal(document, color=color)
