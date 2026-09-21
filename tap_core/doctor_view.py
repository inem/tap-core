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


def _proxy_endpoint(state):
    if not isinstance(state, dict):
        return "unknown"
    if state.get("enabled") is not True:
        return "off"
    return f"{state.get('server', '?')}:{state.get('port', '?')}"


def _proxy_reason(result):
    mismatches = result.get("system_proxy_mismatches")
    if not isinstance(mismatches, list) or not mismatches:
        return "system proxy is not verified"
    details = []
    for item in mismatches:
        if not isinstance(item, dict):
            continue
        details.append(f"{item.get('service', '?')} "
                       f"(HTTP {_proxy_endpoint(item.get('http'))}, "
                       f"HTTPS {_proxy_endpoint(item.get('https'))})")
    active = result.get("active_network_service")
    if result.get("active_system_proxy_verified") is True:
        prefix = f"active service {active or '?'} works, but full system policy is not satisfied"
    else:
        prefix = "full system proxy policy is not satisfied"
    return prefix + ("; mismatched services: " + ", ".join(details) if details else "")


def _bad_reasons(result):
    reasons = []
    if "backend_error" in result:
        reasons.append(("backend", result["backend_error"]))
    if result.get("port_owned") is not True:
        reasons.append(("runtime", "profile does not own the listener"))
    if result.get("port_open") is None:
        reasons.append(("runtime", "listener inspection is unknown"))
    elif result.get("port_open") is not True:
        reasons.append(("runtime", "listener is not open"))
    proxy = result.get("system_proxy_verified")
    if proxy not in (True, "not_used"):
        reasons.append(("routing", _proxy_reason(result) if proxy is False
                        else "system proxy inspection is unknown"))
    capture = result.get("capture") if isinstance(result.get("capture"), dict) else {}
    if capture.get("healthy") is not True:
        reasons.append(("capture", "writer health is not verified"))
    bridge = result.get("bridge") if isinstance(result.get("bridge"), dict) else {}
    if bridge.get("healthy") is not True:
        reasons.append(("bridge", "startup snapshot is not healthy"))
    if bridge.get("hub_liveness") == "failed":
        detail = bridge.get("liveness_error") or "health endpoint did not respond"
        reasons.append(("bridge", f"Hub liveness probe failed: {detail}; inspect components.log and use off/on"))
    elif bridge.get("control_liveness") == "failed":
        detail = bridge.get("liveness_error") or "control endpoint did not respond"
        reasons.append(("bridge", f"control-router probe failed: {detail}; inspect components.log and use off/on"))
    components = result.get("components") if isinstance(result.get("components"), dict) else {}
    if components.get("healthy") is not True:
        detail = components.get("error")
        message = "managed component control plane is not healthy"
        reasons.append(("control", message + (f": {detail}" if detail else "")))
    if result.get("traffic_probe") is not True:
        reasons.append(("traffic", "proxy request probe did not pass"))
    https = result.get("https_decryption") if isinstance(result.get("https_decryption"), dict) else {}
    if https.get("profile_ca_verified") is not True:
        detail = https.get("profile_ca_error") or https.get("error") or https.get("reason") or "not verified"
        reasons.append(("HTTPS decrypt", f"profile-CA probe failed: {detail}"))
    if https.get("system_trust_verified") is not True:
        detail = https.get("system_trust_error") or https.get("error") or https.get("reason") or "not verified"
        reasons.append(("curl trust", f"default trust probe failed: {detail}"))
    if isinstance(result.get("inspection_errors"), dict):
        for name, message in sorted(result["inspection_errors"].items()):
            reasons.append((name, message))
    return reasons


def _limitations(result):
    items = []
    if (result.get("system_proxy_verified") is False
            and result.get("active_system_proxy_verified") is True):
        active = result.get("active_network_service") or "the active service"
        if result.get("network_recovery_pending") is True:
            repair = "run tap off to restore owned routing, then tap on to re-arm every enabled service"
        else:
            repair = (f"rescue route is not owned; disable the manual proxy on {active}, "
                      "then run tap on to adopt every enabled service")
        items.append(("routing policy", repair))
    trust = result.get("ca_trust")
    if isinstance(trust, str) and trust.startswith("not_verified;"):
        items.append(("CA trust", trust.replace("not_verified;", "not verified —", 1)))
    elif result.get("ca_trust_grant_recorded") is False:
        items.append(("CA provenance", "live trust may pass, but no owned finish-setup grant is recorded"))
    bridge = result.get("bridge") if isinstance(result.get("bridge"), dict) else {}
    if (bridge.get("hub_liveness") == "not_checked"
            or bridge.get("control_liveness") == "not_checked"):
        items.append(("bridge", "Hub/control liveness was not checked"))
    background = result.get("background") if isinstance(result.get("background"), dict) else {}
    quarantined = background.get("quarantined")
    if isinstance(quarantined, list) and quarantined:
        items.append(("background", "quarantined packs: " + ", ".join(map(str, quarantined))))
    failures = background.get("verify_failures")
    if isinstance(failures, dict) and failures:
        visible = []
        for name, record in sorted(failures.items()):
            if isinstance(record, dict) and record.get("quarantined"):
                visible.append(f"{name}@{record.get('version', '?')}")
        if visible:
            items.append(("pack integrity", "quarantined after verify failures: " + ", ".join(visible)))
    return items


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
    trust_granted = isinstance(trust, str) and trust.startswith(("granted;", "verified_live;"))
    if isinstance(trust, str):
        trust = (trust.replace("not_verified;", "not verified —", 1)
                 .replace("verified_live;", "verified live —", 1)
                 .replace("granted;", "granted —", 1))
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

    https = result.get("https_decryption") if isinstance(result.get("https_decryption"), dict) else {}
    decrypt = https.get("profile_ca_verified")
    lines.append(_line("  HTTPS decrypt", "good" if decrypt is True else
                       "bad" if decrypt is False else "unknown",
                       "profile CA verified" if decrypt is True else
                       "profile CA failed" if decrypt is False else "not checked", color))
    curl_trust = https.get("system_trust_verified")
    lines.append(_line("  curl trust", "good" if curl_trust is True else
                       "bad" if curl_trust is False else "unknown",
                       "default trust verified" if curl_trust is True else
                       "default trust failed" if curl_trust is False else "not checked", color))

    limitations = _limitations(result)
    if limitations:
        lines.append("Limitations")
        for name, message in limitations:
            lines.append(f"  {_safe(name)}: {_safe(message)}")

    if not healthy:
        reasons = _bad_reasons(result)
        if reasons:
            lines.append("Reasons")
            seen = set()
            for name, message in reasons:
                key = (_safe(name), _safe(message))
                if key in seen:
                    continue
                seen.add(key)
                lines.append(f"  {key[0]}: {key[1]}")

    errors = result.get("inspection_errors")
    if isinstance(errors, dict) and errors:
        lines.append("Inspection errors")
        for name, message in sorted(errors.items()):
            lines.append(f"  {_safe(name)}: {_safe(message)}")
    if result.get("next"):
        lines.append("Next")
        lines.extend(f"  {_safe(line)}" for line in result["next"])
    return "\n".join(lines)
