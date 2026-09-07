"""First #52 semantic-to-terminal slice for one ``tap status`` row.

Each function owns one transformation and is deliberately pure.  The terminal
renderer only knows the render-document vocabulary; TAP-specific conclusions
are complete before lowering reaches it.
"""


STATUS_OBSERVATIONS = "tap.status-observations/v1"
STATUS_SUMMARY = "tap.status-summary/v1"
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


def lower_status_line(view):
    """Lower the TAP-specific view into a renderer-neutral document."""
    if view.get("schema") != STATUS_LINE:
        raise ValueError("unsupported status line")
    return {
        "schema": RENDER_DOCUMENT,
        "layout": {"label_width": 14},
        "blocks": [{"type": "status_row", **view["row"]}],
    }


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
        lines.append(line)
    return "\n".join(lines)


def status_terminal(snapshot, color=False):
    """Run the complete first slice while keeping every stage independently testable."""
    observations = normalize_status_observations(snapshot)
    summary = assess_status_summary(observations)
    view = project_status_line(summary)
    document = lower_status_line(view)
    return render_terminal(document, color=color)
