"""Usage freshness policy (#225) — decisions only.

Pure functions: no clock, no I/O, no scheduling, no provider code. The future
coordinator feeds observed state and the current time in, and gets back a
decision receipt saying whether one provider/account may be refreshed now and
why. Adapters never decide; they execute an approved refresh and report back.

Nothing in Core imports this module yet. See docs/usage-freshness-policy.md.
"""
import hashlib
import json

INF = float("inf")

DEFAULT_POLICY = {
    # Passive activity seen within `active_window` makes the target "active".
    "active_window": 1800,
    "active_interval": 900,      # active client: quota is moving
    "watched_interval": 1800,    # dashboard visible, client not active
    "idle_interval": 21600,      # nothing happening here: decay to 6 h
    "safety_interval": 86400,    # night, or user away: one look a day at most
    "night": {"start_hour": 1, "end_hour": 8},   # local hours, [start, end)
    "away_after": 3600,          # user idle this long counts as away
    "min_manual_interval": 60,   # explicit refresh requests inside this coalesce
    "timeout": 60,
    "in_flight_grace": 30,
    "budget": {"window": 86400, "max": 48, "manual_reserve": 6},
    "backoff": {"base": 300, "factor": 2, "max": 21600},
    "max_per_wake": 1,           # a wake never fans out to every provider
}

REFRESH, SKIP = "refresh", "skip"


def new_state(provider, account=None, adapter=None):
    """State the coordinator keeps per provider/account. `adapter` is the id of
    an admitted live adapter, or None: passive-only targets are never refreshed."""
    return {
        "provider": provider, "account": account, "adapter": adapter,
        "last_authoritative_at": None,   # newest provider-stated quota, from any source
        "last_authoritative_via": None,  # "live-adapter" | "passive-capture" | "legacy-schedule"
        "last_activity_at": None,        # newest passive activity observation
        "in_flight": None,               # {"started_at", "decision_id"}
        "failures": 0,
        "retry_at": None,
        "attempts": [],                  # started_at of live attempts, for the budget
        "manual_requested_at": None,     # explicit user intent; a repaint is not intent
    }


def context(now, utc_offset_seconds=0, online=None, user_idle_seconds=None,
            dashboard_visible=False):
    return {"now": now, "utc_offset_seconds": utc_offset_seconds, "online": online,
            "user_idle_seconds": user_idle_seconds, "dashboard_visible": bool(dashboard_visible)}


def local_hour(ctx):
    return int(((ctx["now"] + ctx["utc_offset_seconds"]) % 86400) // 3600)


def is_night(ctx, policy):
    start, end = policy["night"]["start_hour"], policy["night"]["end_hour"]
    hour = local_hour(ctx)
    return start <= hour < end if start <= end else (hour >= start or hour < end)


def tier(state, ctx, policy):
    """night > away > active > watched > idle. Night and away win over activity
    on purpose: background agents keep working at 3 am; the person reading the
    dashboard does not."""
    now = ctx["now"]
    if is_night(ctx, policy):
        return "night", policy["safety_interval"]
    idle = ctx.get("user_idle_seconds")
    if idle is not None and idle >= policy["away_after"]:
        return "away", policy["safety_interval"]
    seen = state.get("last_activity_at")
    snapshot = state.get("last_authoritative_at")
    # Activity the last snapshot already accounts for does not make the target
    # active again: the provider's number cannot have moved because of it.
    if seen is not None and now - seen <= policy["active_window"] \
            and (snapshot is None or seen > snapshot):
        return "active", policy["active_interval"]
    if ctx.get("dashboard_visible"):
        return "watched", policy["watched_interval"]
    return "idle", policy["idle_interval"]


def budget_used(state, now, policy):
    since = now - policy["budget"]["window"]
    return sum(1 for at in state.get("attempts") or [] if at > since)


def decide(state, ctx, policy=None):
    """One target, one verdict. First matching rule wins; the order is the policy."""
    policy = policy or DEFAULT_POLICY
    now = ctx["now"]
    name, interval = tier(state, ctx, policy)
    seen = state.get("last_authoritative_at")
    age = INF if seen is None else max(0.0, now - seen)
    used = budget_used(state, now, policy)
    manual = state.get("manual_requested_at")
    manual_pending = manual is not None and (seen is None or manual > seen)
    flight = state.get("in_flight")
    stale_flight = False
    if flight:
        stale_flight = now - flight["started_at"] >= policy["timeout"] + policy["in_flight_grace"]

    def verdict(action, reason, next_at=None):
        return _receipt(state, ctx, policy, action, reason, name, interval, age, used,
                        manual_pending, stale_flight, next_at)

    if not state.get("adapter"):
        return verdict(SKIP, "no_adapter")
    if flight and not stale_flight:
        return verdict(SKIP, "in_flight",
                       flight["started_at"] + policy["timeout"] + policy["in_flight_grace"])
    if ctx.get("online") is False:
        return verdict(SKIP, "offline")
    if state.get("retry_at") is not None and now < state["retry_at"]:
        return verdict(SKIP, "backoff", state["retry_at"])
    limit = policy["budget"]["max"] + (policy["budget"]["manual_reserve"] if manual_pending else 0)
    if used >= limit:
        oldest = min(at for at in state["attempts"] if at > now - policy["budget"]["window"])
        return verdict(SKIP, "budget", oldest + policy["budget"]["window"])
    if manual_pending:
        if age < policy["min_manual_interval"]:
            return verdict(SKIP, "fresh_enough", seen + policy["min_manual_interval"])
        return verdict(REFRESH, "manual")
    if age >= interval:
        return verdict(REFRESH, {"night": "safety_wake", "away": "safety_wake"}.get(name, "due_" + name))
    quiet = {"night": "night_quiet", "away": "away_quiet", "idle": "idle_decay"}.get(name)
    if state.get("last_authoritative_via") == "passive-capture" and name in ("active", "watched"):
        quiet = "passive_fresh"
    return verdict(SKIP, quiet or "fresh", seen + interval)


def plan(states, ctx, policy=None):
    """One wake over many targets. Explicit requests all pass; of the rest at most
    `max_per_wake` are approved, most overdue first. The others wait for the next wake."""
    policy = policy or DEFAULT_POLICY
    receipts = [decide(state, ctx, policy) for state in states]
    due = [r for r in receipts if r["action"] == REFRESH and r["reason"] != "manual"]
    due.sort(key=lambda r: (-_overdue(r), r["target"]["provider"], r["target"]["account"] or ""))
    for receipt in due[policy["max_per_wake"]:]:
        receipt["action"], receipt["deferred_reason"] = SKIP, receipt["reason"]
        receipt["reason"] = "coalesced_wake"
        receipt["decision_id"] = _decision_id(receipt)
    return receipts


def _overdue(receipt):
    age, interval = receipt["inputs"]["age"], receipt["inputs"]["interval"]
    return INF if age is None else age / float(interval)


# --- state transitions (pure: each returns a new state) -----------------------
def started(state, receipt, now):
    out = dict(state)
    out["in_flight"] = {"started_at": now, "decision_id": receipt["decision_id"]}
    out["attempts"] = list(state.get("attempts") or []) + [now]
    if receipt["reason"] == "manual":
        out["manual_requested_at"] = None
    return out


def finished(state, outcome, now, policy=None, quota_observed_at=None, retry_after=None):
    """outcome: success | unchanged | error | timeout | rate_limited."""
    policy = policy or DEFAULT_POLICY
    out = dict(state, in_flight=None)
    if outcome in ("success", "unchanged"):
        out.update(failures=0, retry_at=None, last_authoritative_via="live-adapter",
                   last_authoritative_at=max(quota_observed_at or now, state.get("last_authoritative_at") or 0))
        return out
    failures = state.get("failures", 0) + 1
    back = policy["backoff"]
    delay = min(back["base"] * back["factor"] ** (failures - 1), back["max"])
    if outcome == "rate_limited" and retry_after:
        delay = max(delay, retry_after)   # the provider's own retry time is a floor
    out.update(failures=failures, retry_at=now + delay)
    return out


def expire_in_flight(state, now, policy=None):
    """A refresh that never reported back is a timeout, not a free retry."""
    policy = policy or DEFAULT_POLICY
    flight = state.get("in_flight")
    if flight and now - flight["started_at"] >= policy["timeout"] + policy["in_flight_grace"]:
        return finished(state, "timeout", now, policy)
    return state


def observed_quota(state, quota_observed_at, via):
    """Provider-stated quota arrived without us asking (capture saw the client's
    own call, or the transitional scheduled producer wrote it)."""
    if state.get("last_authoritative_at") is not None and quota_observed_at <= state["last_authoritative_at"]:
        return state
    return dict(state, last_authoritative_at=quota_observed_at, last_authoritative_via=via)


def observed_activity(state, observed_at):
    if state.get("last_activity_at") is not None and observed_at <= state["last_activity_at"]:
        return state
    return dict(state, last_activity_at=observed_at)


def requested(state, now):
    """An explicit user action. Rendering, polling or focusing a view must not call this."""
    return dict(state, manual_requested_at=now)


# --- receipt ------------------------------------------------------------------
def _receipt(state, ctx, policy, action, reason, tier_name, interval, age, used,
             manual_pending, stale_flight, next_at):
    receipt = {
        "receipt": "tap.usage-freshness-decision/v1",
        "at": ctx["now"],
        "target": {"provider": state["provider"], "account": state.get("account")},
        "adapter": state.get("adapter"),
        "action": action, "reason": reason, "tier": tier_name,
        "next_eligible_at": next_at,
        "timeout": policy["timeout"] if action == REFRESH else None,
        "stale_in_flight": stale_flight,
        "inputs": {
            "age": None if age == INF else age, "interval": interval,
            "last_authoritative_at": state.get("last_authoritative_at"),
            "last_authoritative_via": state.get("last_authoritative_via"),
            "last_activity_at": state.get("last_activity_at"),
            "failures": state.get("failures", 0), "retry_at": state.get("retry_at"),
            "budget_used": used, "budget_max": policy["budget"]["max"],
            "manual_pending": manual_pending,
            "online": ctx.get("online"), "local_hour": local_hour(ctx),
            "user_idle_seconds": ctx.get("user_idle_seconds"),
            "dashboard_visible": ctx.get("dashboard_visible", False),
        },
    }
    receipt["decision_id"] = _decision_id(receipt)
    return receipt


def _decision_id(receipt):
    blob = json.dumps([receipt["target"], receipt["at"], receipt["action"], receipt["reason"]],
                      sort_keys=True)
    return "d-" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]
