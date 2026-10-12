# SPDX-License-Identifier: GPL-3.0-only
import time
import unittest

from smartac.core import config
from smartac.core import rules as r
from smartac.core import types as t

MIN = 60 * 1000
OFF = t.ACState(False, t.MODE_FAN)


def reading(temp=None, hum=None, outdoor=None):
    return t.Reading(OFF, temp=temp, hum=hum, outdoor_temp=outdoor)


def cond(sensor, op, value):
    return {"sensor": sensor, "op": op, "value": value}


def rule(name, *conditions, confirm=1, interval=0, match="all"):
    return {
        "name": name,
        "when": {"match": match, "conditions": list(conditions)},
        "confirm_reads": confirm,
        "phases": [{"mode": "fan", "fan": "3", "max_min": 20, "until": None}],
        "cooldown_min": 0,
        "min_interval_min": interval,
    }


HOT = rule("hot", cond("temp", ">", 29), confirm=2)
HUMID = rule("humid", cond("hum", ">", 75), confirm=2)


class TestConditions(unittest.TestCase):
    def test_ops_on_tenths(self):
        rd = reading(temp=290)
        self.assertFalse(r.condition_met(cond("temp", ">", 29), rd))
        self.assertTrue(r.condition_met(cond("temp", ">=", 29), rd))
        self.assertTrue(r.condition_met(cond("temp", ">", 28.5), rd))
        self.assertFalse(r.condition_met(cond("temp", "<", 29), rd))
        self.assertTrue(r.condition_met(cond("temp", "<=", 29), rd))

    def test_half_degree_threshold(self):
        self.assertTrue(r.condition_met(cond("outdoor_temp", ">", 31), reading(outdoor=315)))
        self.assertFalse(r.condition_met(cond("outdoor_temp", ">", 31.5), reading(outdoor=315)))
        self.assertTrue(r.condition_met(cond("outdoor_temp", "<", -2), reading(outdoor=-25)))

    def test_humidity_is_whole_percent(self):
        self.assertTrue(r.condition_met(cond("hum", ">", 75), reading(hum=76)))
        self.assertFalse(r.condition_met(cond("hum", ">", 75), reading(hum=75)))
        self.assertTrue(r.condition_met(cond("hum", "<=", 65), reading(hum=65)))

    def test_missing_sensor_is_not_met(self):
        for c in (cond("temp", ">", 0), cond("temp", "<", 100)):
            self.assertFalse(r.condition_met(c, reading(hum=80)))
        self.assertFalse(r.condition_met(cond("temp", "<", 100), t.Reading.unreachable("timeout")))

    def test_bad_condition_is_not_met(self):
        rd = reading(temp=290, hum=80)
        for c in (cond("temp", "==", 29), cond("dew_point", ">", 0), cond("temp", ">", None), {}):
            self.assertFalse(r.condition_met(c, rd))

    def test_match_all_and_any(self):
        cs = [cond("temp", ">", 27), cond("hum", ">", 75)]
        both, one = reading(temp=280, hum=80), reading(temp=280, hum=60)
        self.assertTrue(r.when_met({"match": "all", "conditions": cs}, both))
        self.assertFalse(r.when_met({"match": "all", "conditions": cs}, one))
        self.assertTrue(r.when_met({"match": "any", "conditions": cs}, one))
        self.assertFalse(r.when_met({"match": "any", "conditions": cs}, reading(temp=250, hum=60)))
        self.assertTrue(r.when_met({"conditions": cs}, both))  # "all" by default

    def test_empty_or_unknown_match_is_not_met(self):
        rd = reading(temp=300)
        self.assertFalse(r.when_met({"match": "all", "conditions": []}, rd))
        self.assertFalse(r.when_met({"match": "any", "conditions": []}, rd))
        self.assertFalse(r.when_met({"match": "xor", "conditions": [cond("temp", ">", 20)]}, rd))

    def test_goal(self):
        until = cond("temp", "<=", 26)
        self.assertTrue(r.goal_met(until, reading(temp=260)))
        self.assertFalse(r.goal_met(until, reading(temp=265)))
        self.assertFalse(r.goal_met(until, reading(hum=50)))
        self.assertFalse(r.goal_met(None, reading(temp=200)))


class TestConfirmReads(unittest.TestCase):
    def setUp(self):
        self.sel = r.RuleSelector()

    def test_needs_consecutive_reads(self):
        self.assertIsNone(self.sel.observe([HOT], reading(temp=300), 0))
        self.assertEqual(self.sel.observe([HOT], reading(temp=300), 1 * MIN), 0)

    def test_confirm_one_starts_at_once(self):
        self.assertEqual(self.sel.observe([rule("x", cond("temp", ">", 29))], reading(temp=300), 0), 0)

    def test_miss_resets_count(self):
        self.sel.observe([HOT], reading(temp=300), 0)
        self.sel.observe([HOT], reading(temp=290), 1 * MIN)
        self.assertIsNone(self.sel.observe([HOT], reading(temp=300), 2 * MIN))
        self.assertEqual(self.sel.observe([HOT], reading(temp=300), 3 * MIN), 0)

    def test_missing_sensor_resets_count(self):
        self.sel.observe([HOT], reading(temp=300), 0)
        self.sel.observe([HOT], reading(hum=50), 1 * MIN)  # temp out of range -> None
        self.assertIsNone(self.sel.observe([HOT], reading(temp=300), 2 * MIN))

    def test_unreachable_resets_count(self):
        self.sel.observe([HOT], reading(temp=300), 0)
        self.assertIsNone(self.sel.observe([HOT], t.Reading.unreachable("timeout"), 1 * MIN))
        self.assertIsNone(self.sel.observe([HOT], reading(temp=300), 2 * MIN))
        self.assertEqual(self.sel.observe([HOT], reading(temp=300), 3 * MIN), 0)

    def test_close_reads_do_not_count(self):
        # An extra tick (queued action) right after a poll doesn't confirm a trigger.
        self.sel.observe([HOT], reading(temp=300), 0)
        self.assertIsNone(self.sel.observe([HOT], reading(temp=300), 5 * 1000))
        self.assertIsNone(self.sel.observe([HOT], reading(temp=300), 30 * 1000))
        self.assertEqual(self.sel.observe([HOT], reading(temp=300), 50 * 1000), 0)

    def test_close_read_does_not_reset_either(self):
        self.sel.observe([HOT], reading(temp=300), 0)
        self.sel.observe([HOT], reading(temp=290), 10 * 1000)
        self.assertEqual(self.sel.observe([HOT], reading(temp=300), 1 * MIN), 0)

    def test_confirmed_rule_still_needs_trigger_now(self):
        self.sel.observe([HOT], reading(temp=300), 0)
        self.sel.observe([HOT], reading(temp=300), 1 * MIN)
        self.assertIsNone(self.sel.observe([HOT], reading(temp=290), 1 * MIN + 5000))

    def test_long_gap_breaks_the_run(self):
        self.sel.observe([HOT], reading(temp=300), 0)
        self.assertIsNone(self.sel.observe([HOT], reading(temp=300), 4 * MIN))
        self.assertEqual(self.sel.observe([HOT], reading(temp=300), 5 * MIN), 0)

    def test_gap_limits_are_parameters(self):
        sel = r.RuleSelector(min_gap_ms=20 * 1000, max_gap_ms=60 * 1000)
        sel.observe([HOT], reading(temp=300), 0)
        self.assertEqual(sel.observe([HOT], reading(temp=300), 30 * 1000), 0)
        sel.reset()
        sel.observe([HOT], reading(temp=300), 0)
        self.assertIsNone(sel.observe([HOT], reading(temp=300), 61 * 1000))

    def test_reset_forgets_counts(self):
        self.sel.observe([HOT], reading(temp=300), 0)
        self.sel.reset()
        self.assertIsNone(self.sel.observe([HOT], reading(temp=300), 1 * MIN))

    def test_counts_follow_rule_names(self):
        # A config edit that reorders rules keeps each rule's count.
        self.sel.observe([HOT, HUMID], reading(temp=300, hum=60), 0)
        self.assertEqual(self.sel.observe([HUMID, HOT], reading(temp=300, hum=60), 1 * MIN), 1)

    def test_unknown_rule_starts_from_zero(self):
        self.sel.observe([HOT], reading(temp=300), 0)
        renamed = rule("very_hot", cond("temp", ">", 29), confirm=2)
        self.assertIsNone(self.sel.observe([renamed], reading(temp=300), 1 * MIN))


class TestFirstMatch(unittest.TestCase):
    def setUp(self):
        self.sel = r.RuleSelector()

    def confirm(self, rules, rd, now=0):
        self.sel.observe(rules, rd, now)
        return self.sel.observe(rules, rd, now + MIN)

    def test_first_matching_rule_wins(self):
        self.assertEqual(self.confirm([HOT, HUMID], reading(temp=300, hum=80)), 0)
        self.sel.reset()
        self.assertEqual(self.confirm([HUMID, HOT], reading(temp=300, hum=80)), 0)

    def test_later_rule_when_earlier_not_met(self):
        self.assertEqual(self.confirm([HOT, HUMID], reading(temp=250, hum=80)), 1)

    def test_nothing_met(self):
        self.assertIsNone(self.confirm([HOT, HUMID], reading(temp=250, hum=60)))
        self.assertIsNone(self.confirm([], reading(temp=300, hum=90), 2 * MIN))

    def test_confirming_earlier_rule_keeps_priority(self):
        # humid is confirmed, hot has just started to match: wait for hot instead of starting humid.
        self.sel.observe([HOT, HUMID], reading(temp=250, hum=80), 0)
        self.assertIsNone(self.sel.observe([HOT, HUMID], reading(temp=300, hum=80), 1 * MIN))
        self.assertEqual(self.sel.observe([HOT, HUMID], reading(temp=300, hum=80), 2 * MIN), 0)

    def test_default_day_profile(self):
        rules = config.default_profiles()["profiles"]["day"]["rules"]
        self.assertEqual(self.confirm(rules, reading(temp=300, hum=80)), 0)  # too_hot
        self.sel.reset()
        self.assertEqual(self.confirm(rules, reading(temp=290, hum=76), 5 * MIN), 1)  # humid
        self.sel.reset()
        self.assertIsNone(self.confirm(rules, reading(temp=290, hum=75), 10 * MIN))


class TestMinInterval(unittest.TestCase):
    def setUp(self):
        self.sel = r.RuleSelector()
        self.damp = rule("damp", cond("hum", ">", 80), interval=240)

    def test_blocks_until_interval_passed(self):
        wet = reading(hum=90)
        self.assertEqual(self.sel.observe([self.damp], wet, 0), 0)
        self.sel.started(self.damp, 0)
        self.assertIsNone(self.sel.observe([self.damp], wet, 60 * MIN))
        self.assertIsNone(self.sel.observe([self.damp], wet, 239 * MIN))
        self.assertEqual(self.sel.observe([self.damp], wet, 240 * MIN), 0)

    def test_interval_left(self):
        self.assertEqual(self.sel.interval_left_ms(self.damp, 0), 0)
        self.sel.started(self.damp, 10 * MIN)
        self.assertEqual(self.sel.interval_left_ms(self.damp, 70 * MIN), 180 * MIN)
        self.assertEqual(self.sel.interval_left_ms(self.damp, 250 * MIN), 0)
        self.assertEqual(self.sel.interval_left_ms(HOT, 70 * MIN), 0)

    def test_blocked_rule_falls_through(self):
        backup = rule("backup", cond("hum", ">", 70))
        self.sel.started(self.damp, 0)
        self.assertEqual(self.sel.observe([self.damp, backup], reading(hum=90), 10 * MIN), 1)

    def test_blocked_rule_keeps_counting(self):
        damp2 = rule("damp", cond("hum", ">", 80), confirm=2, interval=240)
        self.sel.started(damp2, 0)
        self.sel.observe([damp2], reading(hum=90), 239 * MIN)
        self.assertEqual(self.sel.observe([damp2], reading(hum=90), 240 * MIN), 0)

    def test_started_requires_fresh_confirmation(self):
        self.sel.observe([HOT], reading(temp=300), 0)
        self.sel.observe([HOT], reading(temp=300), 1 * MIN)
        self.sel.started(HOT, 1 * MIN)
        self.assertIsNone(self.sel.observe([HOT], reading(temp=300), 2 * MIN))
        self.assertEqual(self.sel.observe([HOT], reading(temp=300), 3 * MIN), 0)

    def test_reset_keeps_start_stamps(self):
        self.sel.started(self.damp, 0)
        self.sel.reset()
        self.assertIsNone(self.sel.observe([self.damp], reading(hum=90), 10 * MIN))

    def test_zero_interval_never_blocks(self):
        once = rule("once", cond("temp", ">", 29))
        self.sel.started(once, 0)
        self.assertEqual(self.sel.observe([once], reading(temp=300), 1), 0)


class TestTicksWrap(unittest.TestCase):
    def test_across_wrap(self):
        sel = r.RuleSelector()
        damp = rule("damp", cond("hum", ">", 80), confirm=2, interval=240)
        start = time.ticks_add(0, -100 * MIN)  # 100 min before the wrap
        sel.started(damp, start)
        at = lambda m: time.ticks_add(start, m * MIN)  # noqa: E731
        sel.observe([damp], reading(hum=90), at(238))
        self.assertIsNone(sel.observe([damp], reading(hum=90), at(239)))
        self.assertEqual(sel.observe([damp], reading(hum=90), at(240)), 0)

    def test_old_stamps_are_pruned(self):
        sel = r.RuleSelector()
        sel.started(HOT, 0)
        sel.prune(1439 * MIN)
        self.assertTrue("hot" in sel.starts)
        sel.prune(1440 * MIN)
        self.assertFalse("hot" in sel.starts)

    def test_prune_drops_stale_last_read(self):
        sel = r.RuleSelector()
        sel.observe([HOT], reading(temp=300), 0)
        sel.prune(4 * MIN)
        self.assertIsNone(sel.last_read)
        self.assertEqual(sel.counts, {})
