"""Human presentation of the existing doctor result; no host observations."""

from .status_view import public_status_result, terminal_status


def _safe(value):
    return " ".join("".join(char if char.isprintable() else " " for char in str(value)).split())


def _line(label, state, detail, color):
    symbol = {"good": "●", "bad": "✗", "unknown": "?"}[state]
    if color:
        code = {"good": "32", "bad": "31", "unknown": "33"}[state]
        symbol = f"\x1b[{code}m{symbol}\x1b[0m"
    return f"{label:<14} {symbol} {_safe(detail)}"


def terminal_doctor(result, *, color=False):
    """Render diagnosis and actionable setup lines without changing doctor's result."""
    healthy = result.get("healthy") is True
    lines = [_line("doctor", "good" if healthy else "bad",
                   "healthy" if healthy else "needs attention", color),
             terminal_status(public_status_result(result), color=color), "Checks"]

    if "backend_error" in result:
        lines.append(_line("  backend", "bad", result["backend_error"], color))
    elif result.get("backend_version"):
        lines.append(_line("  backend", "good", result["backend_version"], color))
    else:
        lines.append(_line("  backend", "unknown", "not checked", color))

    ca_file = result.get("ca_file_present")
    lines.append(_line("  CA file", "good" if ca_file is True else
                       "bad" if ca_file is False else "unknown",
                       "present" if ca_file is True else
                       "missing" if ca_file is False else "not checked", color))
    trust = result.get("ca_trust")
    trust_granted = isinstance(trust, str) and trust.startswith("granted;")
    if isinstance(trust, str):
        trust = trust.replace("not_verified;", "not verified —", 1).replace("granted;", "granted —", 1)
    lines.append(_line("  CA trust", "good" if trust_granted else "unknown",
                       trust or "not checked", color))

    if isinstance(result.get("sudoers"), dict):
        ready = result["sudoers"].get("ready")
        lines.append(_line("  proxy rights", "good" if ready is True else
                           "bad" if ready is False else "unknown",
                           "ready" if ready is True else
                           "missing" if ready is False else "not checked", color))

    probe = result.get("traffic_probe")
    lines.append(_line("  traffic", "good" if probe is True else
                       "bad" if probe is False else "unknown",
                       "probe passed" if probe is True else
                       "probe failed" if probe is False else "not checked", color))

    errors = result.get("inspection_errors")
    if isinstance(errors, dict) and errors:
        lines.append("Inspection errors")
        for name, message in sorted(errors.items()):
            lines.append(f"  {_safe(name)}: {_safe(message)}")
    if result.get("next"):
        lines.append("Next")
        lines.extend(f"  {_safe(line)}" for line in result["next"])
    return "\n".join(lines)
