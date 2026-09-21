# Usage freshness policy

Issue #225, parent #222. This is the **decision policy** for the future usage
freshness coordinator, as executable code and deterministic tests:

- `tap_core/freshness_policy.py` — pure functions, no clock, no I/O, no provider code.
- `tests/test_freshness_policy.py` — 35 decision tests.

**The coordinator itself is not built.** Nothing in Core imports this module,
no LaunchAgent, schedule or pack behaviour changes. What is fixed here is *who
decides and by which rules*, so that adapters (#228) and the dashboard (#223)
can be written against a stable boundary.

## Boundary

```
observations ──▶ coordinator state ──▶ decide() ──▶ decision receipt ──▶ adapter.refresh()
 (passive activity,                                  (refresh | skip,        executes ONE approved
  captured quota,                                     reason, inputs)        refresh, reports outcome
  refresh outcomes,
  explicit user request)
```

- **The coordinator owns decisions**: due time, tier, budget, single-flight,
  backoff, wake coalescing.
- **Adapters own nothing but execution**: given an approved receipt and a
  deadline they perform one refresh and return `success | unchanged | error |
  timeout | rate_limited` (+ `retry_after`, `quota_observed_at`). An adapter has
  no timer, no loop and no retry of its own.
- **A target without an admitted adapter is passive-only** and is never
  refreshed, whatever is asked (`no_adapter`). This is the extension point for
  providers still in discovery (Kimi → passive-only per #227; Copilot #224).
- **The UI never causes a request.** Rendering, polling or focusing a view only
  sets `dashboard_visible`, which shortens an interval. Only an explicit user
  action calls `requested()`; repeated requests coalesce.

Records the coordinator reads and writes are the `activity` / `quota` /
`refresh` observations of the usage observation contract (#230, in
`tap-pack-usage`): `last_activity_at` comes from `activity`, `last_authoritative_at`
from `refresh` freshness, and each executed decision becomes a `refresh` row
carrying the receipt's `decision_id`.

## Decision order

`decide(state, ctx)` — the first matching rule wins; the order *is* the policy.

| # | Rule | Verdict |
|---|---|---|
| 1 | no admitted adapter | skip `no_adapter` |
| 2 | a refresh is in flight and not past `timeout + grace` | skip `in_flight` |
| 3 | known offline | skip `offline` — no failure counted, no budget spent |
| 4 | `retry_at` in the future | skip `backoff` — explicit requests do not bypass it |
| 5 | budget for the window used up (explicit requests get a small reserve) | skip `budget` |
| 6 | explicit request pending | `refresh manual`, or skip `fresh_enough` inside `min_manual_interval` |
| 7 | age of the newest provider-stated quota ≥ the tier's interval | `refresh due_active \| due_watched \| due_idle \| safety_wake` |
| 8 | otherwise | skip `fresh \| passive_fresh \| idle_decay \| night_quiet \| away_quiet` with `next_eligible_at` |

### Tiers

| Tier | When | Default interval |
|---|---|---|
| `night` | local hour in `[01, 08)` | 24 h (safety wake only) |
| `away` | user idle ≥ 1 h | 24 h |
| `active` | passive activity within 30 min **and newer than the last quota snapshot** | 15 min |
| `watched` | dashboard visible | 30 min |
| `idle` | none of the above | 6 h |

Night and away outrank activity on purpose: agents keep working at 3 am, the
person reading the dashboard does not. Activity that the last snapshot already
covers does not make a target active — the provider's number cannot have moved
because of it.

### How passive evidence suppresses refresh

`last_authoritative_at` is the newest provider-stated quota **from any source**.
When capture sees the client fetch its own quota (`observed_quota(…,
"passive-capture")`), or the transitional scheduled producer writes one
(`"legacy-schedule"`), the age resets and no live refresh is due. Passive
*activity* never resets the age — it is not quota — it only selects the tier.

### Single flight, timeout, backoff, budget

- One refresh in flight per provider/account; other accounts are independent.
- A refresh that never reports back becomes a `timeout` failure via
  `expire_in_flight()` — not a free retry. Receipts flag `stale_in_flight`.
- Failures back off 5 min × 2ⁿ, capped at 6 h. A provider `retry_after` is a
  floor, never shortened. Any successful look clears the streak.
- Budget: 48 live attempts per 24 h per target, +6 reserved for explicit requests.

### The wake is a safety net

`plan(states, ctx)` evaluates every target but approves at most `max_per_wake`
(1) non-explicit refresh per wake, most overdue first (`age / interval`). The
rest get `coalesced_wake` with the reason they would have had. A coarse periodic
wake therefore never fans out into one request per provider.

## Decision receipt

`tap.usage-freshness-decision/v1`, produced for **every** evaluation, refresh or
skip: `decision_id`, `at`, `target{provider, account}`, `adapter`, `action`,
`reason`, `tier`, `next_eligible_at`, `timeout`, `stale_in_flight`, and the
`inputs` the verdict was based on (age, interval, last authoritative time and
how it was obtained, last activity, failures, retry time, budget used/max,
explicit request pending, online, local hour, user idle, dashboard visible).
`decision_id` is a hash of target, time, action and reason — reproducible.

## Test coverage

| Scenario | Test class |
|---|---|
| active versus idle, activity already covered by the snapshot, never observed | `ActiveVersusIdle` |
| local night (timezone-aware), user away, rare safety wake, explicit request at night | `LocalNight` |
| captured provider answer / transitional producer suppress refresh; passive-only target | `PassiveEvidenceSuppressesRefresh` |
| repaint and visibility flapping create no request; explicit requests coalesce | `UiNeverCausesARequest` |
| single-flight per target, independence across accounts | `SingleFlight` |
| timeout, exponential backoff and cap, rate-limit floor, success clears streak | `TimeoutAndBackoff` |
| offline costs nothing; unknown connectivity is not offline | `Offline` |
| budget, explicit reserve, window expiry | `Budget` |
| one approval per wake, explicit requests exempt, queue drains across wakes | `WakeDoesNotFanOut` |
| receipt completeness, determinism, purity, no clock/network/process access | `Receipt` |

## Transitional state

`usage.meters` keeps its fixed 180 s `usage collect` schedule for Codex. It is
not replaced here. Once it writes `refresh` rows (trigger `legacy-schedule`) the
coordinator sees them as authoritative observations and would simply never find
Codex due — so the two can run side by side until the coordinator path is proven
and the fixed schedule is removed.

## Open

1. Where coordinator state lives and which process owns the wake (background
   host tick vs a dedicated job). Deliberately undecided.
2. Inputs Core cannot supply yet: user idle time, dashboard visibility, online.
   All three are optional (`None` = unknown, treated as "not away / not visible /
   not offline").
3. Interval values are defaults to be tuned against the refresh receipts, not
   measurements. Per-provider overrides are expected from the adapter contract (#228).
4. A known window reset passing (`resets_epoch < now`) could make a snapshot
   predictably stale; not modelled.
5. No jitter: with one approval per wake there is no herd to spread.
