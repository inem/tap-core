# Usage freshness policy

Issue #225, parent #222. This is the **decision policy** for the future usage
freshness coordinator, as executable code and deterministic tests:

- `tap_core/freshness_policy.py` — pure functions, no clock, no I/O, no provider code.
- `tests/test_freshness_policy.py` — 47 decision tests.

Core's existing background host now admits five explicit refresh targets from
`usage.meters:usage collect` (pack 0.22.0+): Codex, Antigravity, Cursor, Factory
and OpenRouter. Each has independent quota freshness, attempts, failures and
backoff. The coordinator checks eligibility at most once a minute, approving
at most one target per wake. Actual refresh intervals remain 15 minutes when
active, 5 h 30 when idle, and the existing night/budget/backoff rules apply.
The wake runs at the existing background cycle boundary; long running scheduled
commands can still delay it. No additional process, timer service or runtime.

Fresh provider quota captured from the client's own traffic suppresses an
active request. Activity, tokens and plan/charge events do not. Decisions and
executions remain in `logs/usage-freshness-decisions.jsonl`; state remains under
`state/background.json` → `usage_freshness.targets`, keyed
`<pack>:<command>/<provider>`.

This remains a transitional Core binding, not the pack-declared adapter contract
tracked in #228. Visibility, user idle and online inputs are not wired. Capture
activity is read for the corresponding client; Antigravity reads native Hub
activity. A client without such evidence remains in the idle tier.

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
| 2a | a refresh is in flight **past** `timeout + grace` and its timeout has not been recorded | skip `stale_in_flight` — never an approval |
| 2b | a refresh is in flight | skip `in_flight` |
| 3 | known offline | skip `offline` — no failure counted, no budget spent |
| 4 | `retry_at` in the future | skip `backoff` — explicit requests do not bypass it |
| 5 | budget for the window used up (explicit requests get a small reserve) | skip `budget` |
| 6 | explicit request pending | `refresh manual`, or skip `fresh_enough` inside `min_manual_interval` |
| 7 | age of the newest provider-stated quota ≥ the tier's interval | `refresh due_active \| due_watched \| due_idle \| safety_wake` |
| 8 | otherwise | skip `fresh \| passive_fresh \| idle_decay \| night_quiet \| away_quiet` with `next_eligible_at` |

### Tiers

| Tier | When | Default interval |
|---|---|---|
| `night` | local hour in `[01, 05)` — **only when the local UTC offset is known** | 24 h (safety wake only) |
| `away` | user idle ≥ 1 h | 24 h |
| `active` | passive activity within 30 min **and newer than the last quota snapshot** | 15 min |
| `watched` | dashboard visible | 30 min |
| `idle` | none of the above | 5 h 30 min |

Night and away outrank activity on purpose: agents keep working at 3 am, the
person reading the dashboard does not. Activity that the last snapshot already
covers does not make a target active — the provider's number cannot have moved
because of it.

### Local time is an input, not an assumption

`context()` has no default offset. The coordinator passes the machine's real
`utc_offset_seconds`; when it cannot, the night gate is **off**
(`inputs.night_gate: "disabled_unknown_timezone"`, `inputs.local_hour: null`)
rather than applied at UTC hours. The away gate, budget and backoff still hold.

### How passive evidence suppresses refresh

`last_authoritative_at` is the newest provider-stated quota **from any source**.
When capture sees the client fetch its own quota (`observed_quota(…,
"passive-capture")`), or the transitional scheduled producer writes one
(`"legacy-schedule"`), the age resets and no live refresh is due. Passive
*activity* never resets the age — it is not quota — it only selects the tier.

### Durable input is not trusted

The coordinator reads stored rows and its own persisted state; it does not
assume each was validated. `observed_activity()`, `observed_quota()` and
`finished()` accept a time only if it is a finite number no more than
`max_clock_skew` ahead of now, and `decide()` / `tier()` ignore a stored value
that fails the same test. Otherwise one `Infinity` — which Python's JSON decoder
accepts — would satisfy `now - seen <= active_window` forever and pin a target to
the 15-minute tier, or make a snapshot look fresh for good. A later real
observation overwrites an impossible stored value.

### Single flight, timeout, backoff, budget

- One refresh in flight per provider/account; other accounts are independent.
- A refresh that never reports back becomes a `timeout` failure via
  `expire_in_flight()` — not a free retry. Until that is recorded, `decide()`
  answers `stale_in_flight` and **cannot approve**: the old request may still be
  on the wire. `advance(state, ctx)` (settle, then decide) and `plan()` (settles
  every target) are the entry points a coordinator uses; `started()` raises on a
  non-refresh receipt or an existing flight. Backoff counts from the moment the
  timeout occurred, not from when it was noticed.
- **A successful call is not freshness.** `finished("success" | "unchanged")`
  advances `last_authoritative_at` only with a finite `quota_observed_at` no more
  than `max_clock_skew` (5 min) ahead of now — matching the `refresh` observation
  contract. Otherwise the look is recorded as the failure `no_quota_observed` and
  backs off. `observed_quota()` applies the same test to passive observations.
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
`inputs` (incl. `night_gate`) the verdict was based on (age, interval, last authoritative time and
how it was obtained, last activity, failures, retry time, budget used/max,
explicit request pending, online, local hour, user idle, dashboard visible).
`decision_id` is a hash of target, time, action and reason — reproducible.

## Test coverage

| Scenario | Test class |
|---|---|
| active versus idle, activity already covered by the snapshot, never observed | `ActiveVersusIdle` |
| local night (timezone-aware), **unknown timezone**, user away, rare safety wake, explicit request at night | `LocalNight` |
| captured provider answer / transitional producer suppress refresh; passive-only target | `PassiveEvidenceSuppressesRefresh` |
| non-finite / far-future times in new input **and** in state already on disk | `MalformedPersistedInput` |
| repaint and visibility flapping create no request; explicit requests coalesce | `UiNeverCausesARequest` |
| single-flight per target, independence across accounts | `SingleFlight` |
| timeout, **stale in-flight never approves**, `started()` guards, **success without quota is a failure**, future timestamps, exponential backoff and cap, rate-limit floor, success clears streak | `TimeoutAndBackoff` |
| offline costs nothing; unknown connectivity is not offline | `Offline` |
| budget, explicit reserve, window expiry | `Budget` |
| one approval per wake, explicit requests exempt, queue drains across wakes | `WakeDoesNotFanOut` |
| receipt completeness, determinism, purity, no clock/network/process access | `Receipt` |

The coordinator itself — two targets of one command, activity from the ledger
tail, one run serving both, ride-along settlement, backoff only for the target
that asked, adoption of the pre-two-target state — is covered by the
`freshness`/`antigravity`/`ride_along` tests in `tests/test_background.py`.

## Transitional state

The background host still wakes for its other scheduled commands. For the one
admitted command it skips the legacy 180-second provider invocation and uses the
coordinator's 15-minute safety wake instead. The compatibility map is explicit
and temporary; #228 replaces it with pack-declared adapters.

### One command, one selected target per execution

The command is invoked as `usage collect CLIENT --max-age SECONDS` through the
existing process-argv-v1 contract. Only that client's reader runs; errors and
successful observations cannot settle a neighbour's budget or backoff. A bare
command is rejected by 0.22.0. Core refuses to invoke older collectors because
they ignored argv and could silently poll every provider.

`freshness.json` is still the live-look sidecar consumed by the app. Its optional
`accounts` map records comparable native account identifiers. Codex app-server
user identity and wham workspace identity are different namespaces, so an email
is not used as an account-equality test.

The capture reader writes `capture-freshness.json`, schema
`tap.usage-passive-quota/v1`, with `clients[provider][account-or-empty]` entries:
`account`, `components`, `observed_at`, `valid_until`. Times are captured response
times, including unchanged quota responses; replay does not renew them. The
pack owns completeness: OpenRouter needs both /key and /credits, each with its
own time; the oldest required component governs freshness. Explicitly partial
windows cannot suppress a full refresh. Passed window resets invalidate the
passive snapshot during normal activity/idle tiers; night/away gates still win.

A single observed account scope can suppress refresh. Multiple captured scopes
or a mismatch with a known comparable live account do not. This is conservative
single-profile coordination, not automatic selection among logged-in accounts.

The coordinator rechecks passive evidence after taking its execution lease, and
the pack checks again immediately before calling the chosen reader. A response
arriving during dispatch can therefore return `passive_fresh` without a live
request. Decision attempts remain conservatively counted if dispatch has already
started. Receipt `ride_along` remains present for compatibility, always false.

Install pack 0.22.0 and this coordinator together. Neither mixed-version order
can fall back to polling all clients. The capture reader bootstraps complete
ordinary snapshots from existing quota history; merged OpenRouter history cannot
prove both endpoint times, so only newly observed component responses qualify.
Live data, budgets and backoff survive. No consumer-app migration is required.

## Open

- #228: pack-declared adapter discovery instead of the transitional binding.
- User idle, dashboard visibility and online inputs; provider-specific intervals.
- Explicit multiple-account selection and identity mapping across native APIs.
- The coordinator still runs at the existing background cycle boundary.

The collector also inspects a bounded 4 MiB tail across at most three retained
capture segments before a request, using the same extractors, when reader
processing lags behind capture. Missing material outside this bound is unknown,
not evidence of freshness. Index writes share a file lock; the existing reader
continues to populate observations beyond this preflight bound.
