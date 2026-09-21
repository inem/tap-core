"""Deterministic decision tests for the usage freshness policy (#225).

No clock, no sleep, no I/O: every test states the time it is talking about."""
import copy
from pathlib import Path
import unittest

from tap_core import freshness_policy as fp

DAY = 1790000000 - (1790000000 % 86400)   # a UTC midnight
P = fp.DEFAULT_POLICY


def at(hour, minute=0):
    return DAY + hour * 3600 + minute * 60


def ctx(now, **kw):
    kw.setdefault("online", True)
    return fp.context(now, **kw)


def target(**kw):
    state = fp.new_state("codex", "user@example.com", adapter="usage.meters:codex-app-server")
    state.update(kw)
    return state


class ActiveVersusIdle(unittest.TestCase):
    def test_active_client_is_due_after_the_active_interval(self):
        now = at(14)
        s = target(last_authoritative_at=now - 1000, last_activity_at=now - 60)
        r = fp.decide(s, ctx(now))
        self.assertEqual((r["action"], r["reason"], r["tier"]), ("refresh", "due_active", "active"))
        self.assertEqual(r["timeout"], P["timeout"])

    def test_active_client_is_left_alone_inside_the_interval(self):
        now = at(14)
        s = target(last_authoritative_at=now - 300, last_activity_at=now - 60)
        r = fp.decide(s, ctx(now))
        self.assertEqual((r["action"], r["reason"]), ("skip", "fresh"))
        self.assertEqual(r["next_eligible_at"], now - 300 + P["active_interval"])

    def test_idle_client_decays_to_the_long_interval(self):
        now = at(14)
        s = target(last_authoritative_at=now - 3 * 3600, last_activity_at=now - 5 * 3600)
        r = fp.decide(s, ctx(now))
        self.assertEqual((r["action"], r["reason"], r["tier"]), ("skip", "idle_decay", "idle"))
        later = fp.decide(s, ctx(now + 4 * 3600))
        self.assertEqual((later["action"], later["reason"]), ("refresh", "due_idle"))

    def test_activity_already_covered_by_the_snapshot_is_not_active(self):
        now = at(14)
        s = target(last_authoritative_at=now - 1000, last_activity_at=now - 1200)
        r = fp.decide(s, ctx(now))
        self.assertEqual((r["action"], r["tier"]), ("skip", "idle"))

    def test_never_observed_target_is_due(self):
        r = fp.decide(target(), ctx(at(14)))
        self.assertEqual((r["action"], r["inputs"]["age"]), ("refresh", None))


class LocalNight(unittest.TestCase):
    def test_night_suppresses_a_refresh_that_would_be_due_by_day(self):
        now = at(3)
        s = target(last_authoritative_at=now - 2 * 3600, last_activity_at=now - 60)
        r = fp.decide(s, ctx(now))
        self.assertEqual((r["action"], r["reason"], r["tier"]), ("skip", "night_quiet", "night"))

    def test_night_is_local_not_utc(self):
        now = at(23, 30)                       # 23:30 UTC = 01:30 at +02:00
        s = target(last_authoritative_at=now - 2 * 3600, last_activity_at=now - 60)
        self.assertEqual(fp.decide(s, ctx(now))["reason"], "due_active")
        local = fp.decide(s, ctx(now, utc_offset_seconds=7200))
        self.assertEqual((local["reason"], local["inputs"]["local_hour"]), ("night_quiet", 1))

    def test_user_away_counts_like_night(self):
        now = at(14)
        s = target(last_authoritative_at=now - 2 * 3600, last_activity_at=now - 60)
        r = fp.decide(s, ctx(now, user_idle_seconds=2 * 3600))
        self.assertEqual((r["action"], r["reason"]), ("skip", "away_quiet"))

    def test_rare_safety_wake_still_happens(self):
        now = at(3)
        s = target(last_authoritative_at=now - 25 * 3600)
        r = fp.decide(s, ctx(now))
        self.assertEqual((r["action"], r["reason"]), ("refresh", "safety_wake"))

    def test_explicit_request_works_at_night(self):
        now = at(3)
        s = fp.requested(target(last_authoritative_at=now - 2 * 3600), now)
        self.assertEqual(fp.decide(s, ctx(now))["reason"], "manual")


class PassiveEvidenceSuppressesRefresh(unittest.TestCase):
    def test_captured_provider_answer_resets_the_due_time(self):
        now = at(14)
        s = target(last_authoritative_at=now - 1000, last_activity_at=now - 60)
        self.assertEqual(fp.decide(s, ctx(now))["action"], "refresh")
        s = fp.observed_quota(s, now - 5, "passive-capture")   # the client asked; capture saw it
        s = fp.observed_activity(s, now - 1)
        r = fp.decide(s, ctx(now))
        self.assertEqual((r["action"], r["reason"]), ("skip", "passive_fresh"))
        self.assertEqual(r["inputs"]["last_authoritative_via"], "passive-capture")

    def test_transitional_scheduled_producer_counts_too(self):
        now = at(14)
        s = fp.observed_quota(target(last_activity_at=now - 60), now - 100, "legacy-schedule")
        self.assertEqual(fp.decide(s, ctx(now))["action"], "skip")

    def test_older_observation_never_moves_freshness_back(self):
        s = fp.observed_quota(target(last_authoritative_at=500.0), 400.0, "passive-capture")
        self.assertEqual(s["last_authoritative_at"], 500.0)

    def test_passive_only_target_is_never_refreshed(self):
        now = at(14)
        kimi = fp.new_state("kimi", None, adapter=None)
        kimi = fp.requested(fp.observed_activity(kimi, now - 10), now)
        r = fp.decide(kimi, ctx(now, dashboard_visible=True))
        self.assertEqual((r["action"], r["reason"]), ("skip", "no_adapter"))


class UiNeverCausesARequest(unittest.TestCase):
    def test_repaints_and_visibility_flaps_do_not_refresh(self):
        now = at(14)
        s = target(last_authoritative_at=now - 600)
        for second in range(0, 600, 7):        # a dashboard redrawing every few seconds
            r = fp.decide(s, ctx(now + second, dashboard_visible=second % 2 == 0))
            self.assertEqual(r["action"], "skip", second)

    def test_visible_dashboard_only_shortens_the_interval(self):
        now = at(14)
        s = target(last_authoritative_at=now - 2000)
        self.assertEqual(fp.decide(s, ctx(now))["reason"], "idle_decay")
        self.assertEqual(fp.decide(s, ctx(now, dashboard_visible=True))["reason"], "due_watched")

    def test_repeated_explicit_requests_coalesce(self):
        now = at(14)
        s = fp.requested(target(last_authoritative_at=now - 600), now)
        first = fp.decide(s, ctx(now))
        self.assertEqual(first["reason"], "manual")
        s = fp.finished(fp.started(s, first, now), "success", now + 2, quota_observed_at=now + 2)
        s = fp.requested(s, now + 10)          # user clicks again 8 s later
        again = fp.decide(s, ctx(now + 10))
        self.assertEqual((again["action"], again["reason"]), ("skip", "fresh_enough"))
        self.assertEqual(again["next_eligible_at"], now + 2 + P["min_manual_interval"])


class SingleFlight(unittest.TestCase):
    def test_one_refresh_in_flight_per_target(self):
        now = at(14)
        s = target(last_authoritative_at=now - 7 * 3600)
        first = fp.decide(s, ctx(now))
        s = fp.started(s, first, now)
        second = fp.decide(fp.requested(s, now + 5), ctx(now + 5))
        self.assertEqual((second["action"], second["reason"]), ("skip", "in_flight"))
        self.assertEqual(s["in_flight"]["decision_id"], first["decision_id"])

    def test_other_accounts_are_independent(self):
        now = at(14)
        a = fp.started(target(), fp.decide(target(), ctx(now)), now)
        b = target(account="other@example.com")
        self.assertEqual(fp.decide(a, ctx(now + 1))["reason"], "in_flight")
        self.assertEqual(fp.decide(b, ctx(now + 1))["action"], "refresh")


class TimeoutAndBackoff(unittest.TestCase):
    def test_a_refresh_that_never_reports_is_a_timeout(self):
        now = at(14)
        s = target(last_authoritative_at=now - 7 * 3600)
        s = fp.started(s, fp.decide(s, ctx(now)), now)
        late = now + P["timeout"] + P["in_flight_grace"]
        self.assertEqual(fp.expire_in_flight(s, late - 1), s)
        self.assertTrue(fp.decide(s, ctx(late))["stale_in_flight"])
        s = fp.expire_in_flight(s, late)
        self.assertIsNone(s["in_flight"])
        self.assertEqual(s["failures"], 1)
        r = fp.decide(s, ctx(late + 1))
        self.assertEqual((r["action"], r["reason"], r["next_eligible_at"]),
                         ("skip", "backoff", late + P["backoff"]["base"]))

    def test_backoff_doubles_and_is_capped(self):
        now, s, delays = at(14), target(), []
        for _ in range(9):
            s = fp.finished(fp.started(s, fp.decide(s, ctx(now)), now), "error", now)
            delays.append(s["retry_at"] - now)
            now = s["retry_at"]
        self.assertEqual(delays[:4], [300, 600, 1200, 2400])
        self.assertEqual(delays[-1], P["backoff"]["max"])

    def test_explicit_request_does_not_bypass_backoff(self):
        now = at(14)
        s = fp.finished(fp.started(target(), fp.decide(target(), ctx(now)), now), "error", now)
        r = fp.decide(fp.requested(s, now + 30), ctx(now + 30))
        self.assertEqual((r["action"], r["reason"]), ("skip", "backoff"))

    def test_rate_limit_retry_after_is_a_floor(self):
        now = at(14)
        s = fp.started(target(), fp.decide(target(), ctx(now)), now)
        s = fp.finished(s, "rate_limited", now, retry_after=5400)
        self.assertEqual(s["retry_at"], now + 5400)
        self.assertEqual(fp.decide(s, ctx(now + 5399))["reason"], "backoff")
        self.assertEqual(fp.decide(s, ctx(now + 5400))["action"], "refresh")

    def test_success_clears_the_failure_streak(self):
        now = at(14)
        s = target(failures=4, retry_at=now - 1)
        s = fp.finished(fp.started(s, fp.decide(s, ctx(now)), now), "unchanged", now + 3,
                        quota_observed_at=now + 3)
        self.assertEqual((s["failures"], s["retry_at"], s["last_authoritative_at"]), (0, None, now + 3))


class Offline(unittest.TestCase):
    def test_offline_skips_without_spending_budget_or_counting_a_failure(self):
        now = at(14)
        s = target(last_authoritative_at=now - 7 * 3600)
        before = copy.deepcopy(s)
        r = fp.decide(s, ctx(now, online=False))
        self.assertEqual((r["action"], r["reason"]), ("skip", "offline"))
        self.assertEqual(s, before)
        self.assertEqual(fp.decide(s, ctx(now + 60, online=True))["action"], "refresh")

    def test_unknown_connectivity_is_not_offline(self):
        r = fp.decide(target(), fp.context(at(14), online=None))
        self.assertEqual(r["action"], "refresh")


class Budget(unittest.TestCase):
    def test_budget_stops_scheduled_refreshes(self):
        now = at(14)
        spent = [now - 100 - i * 60 for i in range(P["budget"]["max"])]
        s = target(attempts=spent, last_authoritative_at=now - 7 * 3600)
        r = fp.decide(s, ctx(now))
        self.assertEqual((r["action"], r["reason"]), ("skip", "budget"))
        self.assertEqual(r["next_eligible_at"], min(spent) + P["budget"]["window"])

    def test_explicit_request_has_a_small_reserve_and_no_more(self):
        now = at(14)
        s = target(attempts=[now - 100] * P["budget"]["max"], last_authoritative_at=now - 3600)
        self.assertEqual(fp.decide(fp.requested(s, now), ctx(now))["reason"], "manual")
        full = P["budget"]["max"] + P["budget"]["manual_reserve"]
        s = target(attempts=[now - 100] * full, last_authoritative_at=now - 3600)
        self.assertEqual(fp.decide(fp.requested(s, now), ctx(now))["reason"], "budget")

    def test_old_attempts_leave_the_window(self):
        now = at(14)
        s = target(attempts=[now - P["budget"]["window"] - 1] * 100)
        self.assertEqual(fp.decide(s, ctx(now))["inputs"]["budget_used"], 0)


class WakeDoesNotFanOut(unittest.TestCase):
    def providers(self, now):
        return [target(last_authoritative_at=now - 7 * 3600),
                dict(fp.new_state("claude", "org-1", adapter="x"), last_authoritative_at=now - 30 * 3600),
                dict(fp.new_state("cursor", "u", adapter="y"), last_authoritative_at=now - 8 * 3600),
                fp.new_state("copilot", None, adapter=None)]

    def test_one_wake_approves_the_most_overdue_target_only(self):
        now = at(14)
        receipts = fp.plan(self.providers(now), ctx(now))
        approved = [r["target"]["provider"] for r in receipts if r["action"] == "refresh"]
        self.assertEqual(approved, ["claude"])
        waiting = {r["target"]["provider"]: (r["reason"], r.get("deferred_reason")) for r in receipts
                   if r["action"] == "skip"}
        self.assertEqual(waiting, {"codex": ("coalesced_wake", "due_idle"),
                                   "cursor": ("coalesced_wake", "due_idle"),
                                   "copilot": ("no_adapter", None)})

    def test_explicit_requests_are_not_held_back_by_the_wake_limit(self):
        now = at(14)
        states = self.providers(now)
        states[0] = fp.requested(states[0], now)
        approved = sorted(r["target"]["provider"] for r in fp.plan(states, ctx(now)) if r["action"] == "refresh")
        self.assertEqual(approved, ["claude", "codex"])

    def test_successive_wakes_drain_the_queue_one_by_one(self):
        now, states, order = at(14), self.providers(at(14)), []
        for wake in range(4):
            tick = now + wake * 300
            receipts = fp.plan(states, ctx(tick))
            for index, r in enumerate(receipts):
                if r["action"] == "refresh":
                    order.append(r["target"]["provider"])
                    states[index] = fp.finished(fp.started(states[index], r, tick), "success",
                                                tick + 1, quota_observed_at=tick + 1)
        self.assertEqual(order, ["claude", "cursor", "codex"])


class Receipt(unittest.TestCase):
    def test_receipt_says_why_in_both_directions(self):
        now = at(14)
        for state in (target(last_authoritative_at=now - 300), target(last_authoritative_at=now - 7 * 3600)):
            r = fp.decide(state, ctx(now, dashboard_visible=True))
            self.assertEqual(r["receipt"], "tap.usage-freshness-decision/v1")
            for key in ("decision_id", "at", "target", "adapter", "action", "reason", "tier",
                        "next_eligible_at", "inputs"):
                self.assertIn(key, r)
            for key in ("age", "interval", "last_authoritative_at", "last_activity_at", "failures",
                        "budget_used", "budget_max", "online", "local_hour", "dashboard_visible"):
                self.assertIn(key, r["inputs"])

    def test_decisions_are_deterministic_and_pure(self):
        now = at(14)
        s = target(last_authoritative_at=now - 7 * 3600)
        before = copy.deepcopy(s)
        self.assertEqual(fp.decide(s, ctx(now)), fp.decide(s, ctx(now)))
        self.assertEqual(s, before)
        self.assertNotEqual(fp.decide(s, ctx(now))["decision_id"], fp.decide(s, ctx(now + 1))["decision_id"])

    def test_module_has_no_clock_network_or_process_access(self):
        source = Path(fp.__file__).read_text(encoding="utf-8")
        for forbidden in ("import time", "import subprocess", "import socket", "urllib", "import os",
                          "datetime", "import threading", "open("):
            self.assertNotIn(forbidden, source, forbidden)


if __name__ == "__main__":
    unittest.main()
