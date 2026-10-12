# SPDX-License-Identifier: GPL-3.0-only
# Rule evaluation (docs/idea.md, "Profiles" and "Safety"): conditions, `until` goals, and picking the
# rule to start: first match wins, after `confirm_reads` consecutive reads and outside `min_interval_min`.
# Pure: time is passed in as monotonic ms and only compared through ticks_since (ADR 0004).
from smartac.core.types import TENTHS_SENSORS, tenths, ticks_since

_OPS = {
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
}

# Reads count toward confirm_reads only when they are about a poll apart ("1 minute apart"): an extra
# read from an action-triggered tick doesn't count, and a longer gap breaks the run of consecutive reads.
MIN_GAP_MS = 50 * 1000
MAX_GAP_MS = 180 * 1000
# Longest min_interval_min the validation allows (1440). Older start stamps are dropped, well before
# ticks_diff wraps (about 6 days).
MAX_INTERVAL_MS = 1440 * 60 * 1000


def condition_met(cond, reading):
    # {"sensor", "op", "value"} against a Reading. Value is in degrees C (temp sensors) or percent.
    # A missing sensor or an unknown op is "not met", so bad data never starts a cycle.
    op = _OPS.get(cond.get("op"))
    sensor = cond.get("sensor")
    value = reading.sensor(sensor)
    if op is None or value is None or cond.get("value") is None:
        return False
    threshold = tenths(cond["value"]) if sensor in TENTHS_SENSORS else cond["value"]
    return op(value, threshold)


def when_met(when, reading):
    # A rule trigger: {"match": "all" | "any", "conditions": [...]}. No conditions is "not met".
    conditions = when.get("conditions") or ()
    if not conditions:
        return False
    match = when.get("match", "all")
    if match == "all":
        return all(condition_met(c, reading) for c in conditions)
    if match == "any":
        return any(condition_met(c, reading) for c in conditions)
    return False


def goal_met(until, reading):
    # A phase's optional `until` goal; None means the phase runs its full max_min.
    return until is not None and condition_met(until, reading)


class RuleSelector:
    # Tracks confirm_reads counts and cycle starts for the rules of the active profile, keyed by rule name
    # (so an edit that reorders rules keeps them). The controller feeds it the readings it wants judged
    # (IDLE), calls started() when it starts a cycle, and reset() when judging stops or the profile changes.

    def __init__(self, min_gap_ms=MIN_GAP_MS, max_gap_ms=MAX_GAP_MS):
        self.min_gap_ms = min_gap_ms
        self.max_gap_ms = max_gap_ms
        self.counts = {}  # rule name -> consecutive counted reads with the trigger met
        self.starts = {}  # rule name -> ms of the last cycle start
        self.last_read = None  # ms of the last counted read

    def reset(self):
        # Forget the confirm counts; start stamps stay, so min_interval still holds.
        self.counts = {}
        self.last_read = None

    def prune(self, now):
        # Drop stamps too old to matter (or from before a ticks wrap). Called from observe(); the controller
        # should also call it on ticks where it doesn't observe, so stamps never live long enough to wrap.
        for name in list(self.starts):
            age = ticks_since(now, self.starts[name])
            if age < 0 or age >= MAX_INTERVAL_MS:
                del self.starts[name]
        if self.last_read is not None:
            age = ticks_since(now, self.last_read)
            if age < 0 or age > self.max_gap_ms:
                self.reset()

    def interval_left_ms(self, rule, now):
        # Milliseconds until min_interval_min allows this rule to start again; 0 if it may start now.
        then = self.starts.get(rule["name"])
        if then is None:
            return 0
        left = rule.get("min_interval_min", 0) * 60 * 1000 - ticks_since(now, then)
        return left if left > 0 else 0

    def observe(self, rules, reading, now):
        # Count this reading, then return the index of the rule to start, or None.
        # Rules are checked in order:
        #   trigger not met               -> next rule
        #   met, blocked by min_interval  -> next rule (a later rule may run instead)
        #   met, not yet confirmed        -> None (wait: a confirming rule keeps its priority)
        #   met and confirmed             -> this rule
        if not reading.reachable:
            self.reset()
            return None
        self.prune(now)
        if self.last_read is None or ticks_since(now, self.last_read) >= self.min_gap_ms:
            self.last_read = now
            counts = {}
            for rule in rules:
                name = rule["name"]
                counts[name] = self.counts.get(name, 0) + 1 if when_met(rule["when"], reading) else 0
            self.counts = counts

        for i, rule in enumerate(rules):
            if not when_met(rule["when"], reading):
                continue
            if self.interval_left_ms(rule, now):
                continue
            if self.counts.get(rule["name"], 0) < rule.get("confirm_reads", 1):
                return None
            return i
        return None

    def started(self, rule, now):
        # A cycle of this rule started (automatically or from "run"). Triggers must be confirmed afresh.
        self.starts[rule["name"]] = now
        self.reset()
