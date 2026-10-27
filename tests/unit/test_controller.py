# SPDX-License-Identifier: GPL-3.0-only
import json
import time
import unittest

from smartac.core import config
from smartac.core import controller as c
from smartac.core import types as t

MIN = 60 * 1000
OFF_AC = t.ACState(False, "cool", 260, "auto")
COOL = t.ACState(True, "cool", 260, "auto")
FAN3 = t.ACState(True, "fan", None, "3")
FAN3_OFF = t.ACState(False, "fan", None, "3")
DRY = t.ACState(True, "dry", None, "auto")
HUMAN = t.ACState(True, "cool", 240, "2")  # what a remote press might set


def cond(sensor, op, value):
    return {"sensor": sensor, "op": op, "value": value}


def phase(mode, max_min, fan="auto", until=None, target=None):
    p = {"mode": mode, "fan": fan, "max_min": max_min, "until": until}
    if target is not None:
        p["target_c"] = target
    return p


def rule(name, when, phases, cooldown=0, confirm=1, interval=0):
    return {
        "name": name,
        "when": {"match": "all", "conditions": [when]},
        "confirm_reads": confirm,
        "phases": phases,
        "cooldown_min": cooldown,
        "min_interval_min": interval,
    }


# temp > 29: cool to 26 for up to 30 min (until temp <= 26), then fan 3 for 20 min; cooldown 30.
HOT = rule(
    "hot",
    cond("temp", ">", 29),
    [phase("cool", 30, until=cond("temp", "<=", 26), target=26), phase("fan", 20, fan="3")],
    cooldown=30,
)
# hum > 75: dry 30 min, no fan phase, no cooldown.
DAMP = rule("damp", cond("hum", ">", 75), [phase("dry", 30)])


def summary(cmds):
    # Commands as comparable tuples: ("write", ACState), ("event", kind), ("persist",).
    out = []
    for kind, payload in cmds:
        if kind == t.CMD_WRITE:
            out.append(("write", payload))
        elif kind == t.CMD_EVENT:
            out.append(("event", payload[0]))
        else:
            out.append(("persist",))
    return out


def writes(cmds):
    return [p for k, p in cmds if k == t.CMD_WRITE]


def events(cmds):
    return [p[0] for k, p in cmds if k == t.CMD_EVENT]


def msgs(cmds):
    return [p[1] for k, p in cmds if k == t.CMD_EVENT]


class Rig:
    # A controller, a fake clock and a fake AC that applies writes (unless apply is False).
    def __init__(self, rules=None, active="test", snapshot=None, start=0, ac=OFF_AC, **settings):
        self.profiles = {
            "version": 1,
            "active": active,
            "profiles": {
                "test": {"label": "Test", "rules": rules or [HOT, DAMP]},
                "other": {"label": "Other", "rules": [DAMP]},
            },
        }
        self.settings = config.default_settings()
        self.settings.update(settings)
        self.now = start
        self.ac = ac
        self.temp = 250
        self.hum = 60
        self.apply = True
        self.reachable = True
        self.snap = None
        self.ctl = c.Controller(self.profiles, self.settings, start, snapshot)

    @property
    def state(self):
        return self.ctl.state

    def step(self, secs=60, actions=(), readback=False):
        self.now = time.ticks_add(self.now, secs * 1000)
        if self.reachable:
            rd = t.Reading(self.ac, temp=self.temp, hum=self.hum)
        else:
            rd = t.Reading.unreachable("timeout")
        cmds = self.ctl.step(self.now, rd, actions, readback)
        for kind, payload in cmds:
            if kind == t.CMD_WRITE and self.apply:
                self.ac = payload
            elif kind == t.CMD_PERSIST:
                self.snap = json.loads(json.dumps(payload))  # what state.json would hold
        return cmds

    def poll(self, actions=()):
        return self.step(60, actions)

    def polls(self, n):
        cmds = []
        for _ in range(n):
            cmds += self.poll()
        return cmds

    def act(self, **action):
        # An action-triggered tick, a second after the last one.
        return self.step(1, [action])

    def readback(self):
        return self.step(5, readback=True)

    def start_hot(self):
        # Start HOT and confirm its cool phase; the room is still warm afterwards.
        self.temp = 300
        cmds = self.poll()
        self.readback()
        self.temp = 280
        return cmds

    def status(self):
        return self.ctl.status(self.now)


class TestCycleStart(unittest.TestCase):
    def test_start_writes_phase_zero(self):
        rig = Rig()
        self.assertEqual(rig.poll(), [])
        rig.temp = 300
        cmds = rig.poll()
        self.assertEqual(summary(cmds), [("event", t.EV_CYCLE_START), ("write", COOL), ("persist",)])
        self.assertEqual(rig.state, t.RUNNING)
        self.assertEqual(rig.snap["written"], [True, "cool", 260, "auto"])
        self.assertEqual(summary(rig.readback()), [("persist",)])
        self.assertEqual(rig.snap["expected"], [True, "cool", 260, "auto"])
        self.assertIsNone(rig.snap["written"])
        self.assertEqual(rig.poll(), [])

    def test_confirm_reads(self):
        rig = Rig(rules=[rule("hot2", cond("temp", ">", 29), [phase("fan", 10)], confirm=2)])
        rig.temp = 300
        self.assertEqual(rig.poll(), [])
        self.assertEqual(writes(rig.poll()), [t.ACState(True, "fan", None, "auto")])

    def test_dry_has_no_target(self):
        rig = Rig()
        rig.hum = 80
        self.assertEqual(writes(rig.poll()), [DRY])

    def test_first_match_wins(self):
        rig = Rig()
        rig.temp, rig.hum = 300, 80
        self.assertEqual(writes(rig.poll()), [COOL])
        self.assertEqual(rig.snap["cycle"]["rule"]["name"], "hot")

    def test_ac_on_in_idle_is_manual(self):
        rig = Rig()
        rig.ac = HUMAN
        cmds = rig.poll()
        self.assertEqual(summary(cmds), [("event", t.EV_MANUAL), ("persist",)])
        self.assertEqual(rig.state, t.MANUAL)

    def test_off_profile_does_nothing(self):
        rig = Rig(active="off")
        rig.temp = 300
        self.assertEqual(rig.polls(3), [])
        rig.ac = HUMAN
        self.assertEqual(rig.polls(200), [])
        self.assertEqual(rig.state, t.IDLE)

    def test_min_interval(self):
        once = rule("once", cond("temp", ">", 29), [phase("fan", 5)], interval=60)
        rig = Rig(rules=[once])
        rig.temp = 300
        rig.poll()
        rig.polls(6)  # fan 5 min, off, confirmed
        self.assertEqual(rig.state, t.IDLE)
        self.assertEqual(rig.polls(53), [])  # started at minute 1: blocked until minute 61
        self.assertEqual(events(rig.poll()), [t.EV_CYCLE_START])


class TestPhases(unittest.TestCase):
    def test_goal_ends_phase(self):
        rig = Rig()
        rig.start_hot()
        self.assertEqual(rig.polls(5), [])
        rig.temp = 260
        cmds = rig.poll()
        self.assertEqual(summary(cmds), [("event", t.EV_PHASE), ("write", FAN3), ("persist",)])
        self.assertEqual(rig.snap["cycle"]["phase"], 1)

    def test_any_goal_ends_phase(self):
        # Two goals: whichever is met first ends the phase, and the event names it.
        humid = rule("humid", cond("hum", ">", 75), [
            phase("cool", 40, until=[cond("hum", "<=", 65), cond("temp", "<=", 25)], target=26), phase("fan", 20)])
        for sensor, value, goal in (("hum", 65, "hum <= 65"), ("temp", 250, "temp <= 25")):
            rig = Rig([humid])
            rig.hum, rig.temp = 80, 270
            rig.poll()
            rig.readback()
            rig.hum = 70
            self.assertEqual(rig.status()["cycle"]["goal"], "hum <= 65 or temp <= 25")
            self.assertEqual(rig.polls(5), [])
            setattr(rig, sensor, value)
            cmds = rig.poll()
            self.assertEqual(events(cmds), [t.EV_PHASE])
            self.assertIn("(goal %s)" % goal, msgs(cmds)[0])

    def test_max_min_ends_phase(self):
        rig = Rig()
        rig.start_hot()
        self.assertEqual(rig.polls(29), [])
        self.assertEqual(writes(rig.poll()), [FAN3])

    def test_goal_not_checked_on_readback(self):
        rig = Rig()
        rig.start_hot()
        rig.temp = 250
        self.assertEqual(rig.readback(), [])
        self.assertEqual(writes(rig.poll()), [FAN3])

    def test_last_phase_then_off_then_cooldown(self):
        rig = Rig()
        rig.start_hot()
        rig.temp = 260
        rig.poll()
        rig.readback()
        self.assertEqual(rig.polls(19), [])
        cmds = rig.poll()
        self.assertEqual(summary(cmds), [("write", FAN3_OFF), ("persist",)])
        self.assertEqual(rig.state, t.STOPPING)
        rig.readback()
        self.assertEqual(rig.state, t.STOPPING)  # read-back confirms; the next poll ends the cycle
        self.assertEqual(summary(rig.poll()), [("event", t.EV_CYCLE_END), ("persist",)])
        self.assertEqual(rig.state, t.COOLDOWN)
        self.assertEqual(rig.polls(29), [])
        self.assertEqual(summary(rig.poll()), [("persist",)])
        self.assertEqual(rig.state, t.IDLE)

    def test_no_cooldown_goes_idle(self):
        rig = Rig()
        rig.hum = 80
        rig.poll()
        rig.hum = 70
        rig.polls(30)
        self.assertEqual(rig.state, t.STOPPING)
        self.assertEqual(events(rig.poll()), [t.EV_CYCLE_END])
        self.assertEqual(rig.state, t.IDLE)

    def test_cooldown_blocks_triggers(self):
        rig = Rig()
        rig.start_hot()
        rig.polls(51)  # 30 cool + 20 fan + off confirmed
        self.assertEqual(rig.state, t.COOLDOWN)
        rig.temp = 300
        self.assertEqual(rig.polls(29), [])
        self.assertEqual(events(rig.poll()), [t.EV_CYCLE_START])  # cooldown over, trigger met at once


class TestHardCap(unittest.TestCase):
    def test_off_at_once_without_fan(self):
        rig = Rig(hard_cap_min=30)
        rig.start_hot()
        self.assertEqual(rig.polls(29), [])
        cmds = rig.poll()
        self.assertEqual(summary(cmds), [("event", t.EV_HARD_CAP), ("write", OFF_AC), ("persist",)])
        self.assertEqual(rig.state, t.STOPPING)

    def test_counts_finish_phases_too(self):
        rig = Rig(hard_cap_min=40)
        rig.start_hot()
        rig.polls(30)  # fan phase starts at minute 30
        rig.readback()
        self.assertEqual(rig.polls(9), [])
        self.assertEqual(events(rig.poll()), [t.EV_HARD_CAP])


class TestConfigCopy(unittest.TestCase):
    def test_running_cycle_keeps_its_copy(self):
        rig = Rig()
        rig.start_hot()
        new = config.default_settings()
        new["hard_cap_min"] = 30
        rules = [rule("hot", cond("temp", ">", 29), [phase("cool", 5, target=24)], cooldown=0)]
        profiles = {"version": 1, "active": "test", "profiles": {"test": {"label": "T", "rules": rules}}}
        rig.ctl.configure(profiles, new)
        HOT["phases"][0]["max_min"] = 1  # even a change in place doesn't reach the copy
        try:
            self.assertEqual(rig.polls(29), [])
            self.assertEqual(writes(rig.poll()), [FAN3])
        finally:
            HOT["phases"][0]["max_min"] = 30
        rig.polls(51)
        self.assertEqual(rig.state, t.IDLE)  # the old rule's cooldown ran
        rig.temp = 300
        self.assertEqual(writes(rig.poll()), [t.ACState(True, "cool", 240, "auto")])

    def test_settings_copied_at_start(self):
        rig = Rig()
        rig.start_hot()
        rig.settings["hard_cap_min"] = 30
        rig.settings["dry_run"] = True
        self.assertEqual(writes(rig.polls(30)), [FAN3])


class TestReadback(unittest.TestCase):
    def setUp(self):
        self.rig = Rig()
        self.rig.apply = False
        self.rig.temp = 300

    def test_retry_once_then_manual(self):
        rig = self.rig
        rig.poll()
        self.assertEqual(rig.readback(), [])
        self.assertEqual(summary(rig.poll()), [("write", COOL)])  # retried, same snapshot
        cmds = rig.readback()
        self.assertEqual(summary(cmds), [("event", t.EV_WRITE_FAILED), ("persist",)])
        self.assertEqual(rig.state, t.MANUAL)

    def test_without_readback(self):
        rig = self.rig
        rig.poll()
        self.assertEqual(writes(rig.poll()), [COOL])
        self.assertEqual(events(rig.poll()), [t.EV_WRITE_FAILED])
        self.assertEqual(rig.state, t.MANUAL)

    def test_late_apply_is_confirmed(self):
        rig = self.rig
        rig.poll()
        rig.readback()
        rig.ac = COOL
        self.assertEqual(summary(rig.poll()), [("persist",)])
        self.assertEqual(rig.snap["expected"], [True, "cool", 260, "auto"])

    def test_retry_applied(self):
        rig = self.rig
        rig.poll()
        rig.readback()
        rig.apply = True
        rig.poll()
        self.assertEqual(summary(rig.readback()), [("persist",)])
        self.assertEqual(rig.state, t.RUNNING)
        rig.poll()
        rig.apply = False
        rig.temp = 250
        rig.poll()  # the fan write fails once: its count starts from zero
        rig.readback()
        self.assertEqual(writes(rig.poll()), [FAN3])
        self.assertEqual(rig.state, t.RUNNING)

    def test_human_change_after_write(self):
        rig = self.rig
        rig.poll()
        rig.ac = HUMAN
        self.assertEqual(events(rig.poll()), [t.EV_MANUAL])
        self.assertEqual(rig.state, t.MANUAL)

    def test_readback_mismatch_is_not_human(self):
        rig = self.rig
        rig.poll()
        rig.ac = t.ACState(True, "fan", None, "auto")  # e.g. half applied
        self.assertEqual(rig.readback(), [])
        self.assertEqual(rig.state, t.RUNNING)
        rig.ac = COOL
        self.assertEqual(summary(rig.poll()), [("persist",)])

    def test_mismatch_while_owned(self):
        rig = Rig()
        rig.start_hot()
        rig.poll()
        rig.ac = HUMAN
        cmds = rig.poll()
        self.assertEqual(summary(cmds), [("event", t.EV_MANUAL), ("persist",)])
        self.assertEqual(rig.state, t.MANUAL)
        self.assertIsNone(rig.snap["cycle"])


class TestStopping(unittest.TestCase):
    def stopping(self):
        rig = Rig()
        rig.start_hot()
        rig.act(action="stop_now")
        rig.readback()
        return rig

    def test_retries_off_every_poll(self):
        rig = Rig()
        rig.start_hot()
        rig.apply = False
        self.assertEqual(writes(rig.act(action="stop_now")), [OFF_AC])
        self.assertEqual(rig.readback(), [])
        self.assertEqual(summary(rig.poll()), [("write", OFF_AC)])
        self.assertEqual(summary(rig.readback()), [("event", t.EV_WRITE_FAILED)])
        for _ in range(5):
            self.assertEqual(summary(rig.poll()), [("write", OFF_AC)])
        self.assertEqual(rig.state, t.STOPPING)
        rig.apply = True
        rig.poll()
        self.assertEqual(events(rig.poll()), [t.EV_CYCLE_END])
        self.assertEqual(rig.state, t.COOLDOWN)

    def test_human_on_state_is_manual(self):
        rig = Rig()
        rig.start_hot()
        rig.apply = False
        rig.act(action="stop_now")
        rig.ac = HUMAN
        self.assertEqual(events(rig.poll()), [t.EV_MANUAL])
        self.assertEqual(rig.state, t.MANUAL)

    def test_off_by_anyone_ends_it(self):
        rig = Rig()
        rig.start_hot()
        rig.apply = False
        rig.act(action="stop_now")
        rig.ac = t.ACState(False, "dry", None, "1")
        self.assertEqual(events(rig.poll()), [t.EV_CYCLE_END])
        self.assertEqual(rig.state, t.COOLDOWN)

    def test_unreachable_waits(self):
        rig = self.stopping()
        rig.reachable = False
        rig.polls(2)
        self.assertEqual(rig.state, t.COOLDOWN)  # confirmed by the read-back: the next poll ends it
        rig = Rig()
        rig.start_hot()
        rig.reachable = False
        self.assertEqual(summary(rig.act(action="stop_now")), [("persist",)])
        self.assertEqual(rig.state, t.STOPPING)
        rig.reachable = True
        self.assertEqual(writes(rig.poll()), [OFF_AC])


class TestManual(unittest.TestCase):
    def manual(self, ac=HUMAN, **settings):
        rig = Rig(**settings)
        rig.ac = ac
        rig.poll()
        self.assertEqual(rig.state, t.MANUAL)
        return rig

    def test_hold_then_takeover(self):
        rig = self.manual(manual_hold_min=10)
        self.assertEqual(rig.polls(9), [])
        cmds = rig.poll()
        fan2 = t.ACState(True, "fan", None, "2")
        self.assertEqual(summary(cmds), [("event", t.EV_TAKEOVER), ("write", fan2), ("persist",)])
        self.assertEqual(rig.state, t.TAKEOVER)

    def test_change_restarts_hold(self):
        rig = self.manual(manual_hold_min=10)
        rig.polls(8)
        rig.ac = t.ACState(True, "cool", 230, "2")
        rig.polls(10)
        self.assertEqual(rig.state, t.MANUAL)
        self.assertEqual(rig.status()["manual_left_s"], 60)
        self.assertEqual(events(rig.poll()), [t.EV_TAKEOVER])

    def test_off_streak_releases(self):
        rig = self.manual()
        rig.ac = OFF_AC
        self.assertEqual(rig.polls(30), [])
        self.assertEqual(summary(rig.poll()), [("persist",)])
        self.assertEqual(rig.state, t.IDLE)

    def test_on_breaks_off_streak(self):
        rig = self.manual()
        rig.ac = OFF_AC
        rig.polls(20)
        rig.ac = HUMAN
        rig.poll()
        rig.ac = OFF_AC
        rig.polls(30)
        self.assertEqual(rig.state, t.MANUAL)
        rig.poll()
        self.assertEqual(rig.state, t.IDLE)

    def test_hold_over_with_ac_off(self):
        rig = self.manual(manual_hold_min=10, manual_release_off_min=60)
        rig.polls(5)
        rig.ac = OFF_AC
        rig.polls(10)
        self.assertEqual(rig.state, t.MANUAL)
        self.assertEqual(summary(rig.poll()), [("persist",)])  # hold restarted at the off press
        self.assertEqual(rig.state, t.IDLE)

    def test_no_selection_while_manual(self):
        rig = self.manual()
        rig.temp = 300
        self.assertEqual(rig.polls(100), [])


class TestTakeover(unittest.TestCase):
    def takeover(self, ac, **settings):
        rig = Rig(manual_hold_min=10, **settings)
        rig.ac = ac
        rig.polls(10)
        return rig, rig.poll()

    def test_fan_with_humans_speed_then_off_then_idle(self):
        rig, cmds = self.takeover(HUMAN)
        fan2 = t.ACState(True, "fan", None, "2")
        self.assertEqual(writes(cmds), [fan2])
        rig.readback()
        self.assertEqual(rig.polls(19), [])
        self.assertEqual(writes(rig.poll()), [t.ACState(False, "fan", None, "2")])
        rig.readback()
        self.assertEqual(summary(rig.poll()), [("event", t.EV_CYCLE_END), ("persist",)])
        self.assertEqual(rig.state, t.IDLE)  # no cooldown after a takeover

    def test_unknown_fan_is_auto(self):
        rig, cmds = self.takeover(t.ACState(True, "dry", None, None))
        self.assertEqual(writes(cmds), [t.ACState(True, "fan", None, "auto")])

    def test_fan_run_is_left_alone(self):
        rig, cmds = self.takeover(FAN3)
        self.assertEqual(cmds, [])
        self.assertEqual(rig.polls(200), [])
        self.assertEqual(rig.state, t.MANUAL)
        self.assertIsNone(rig.status()["manual_left_s"])
        rig.ac = t.ACState(False, "fan", None, "3")
        rig.polls(30)
        rig.poll()
        self.assertEqual(rig.state, t.IDLE)

    def test_fan_run_changed_to_cool_is_taken_over(self):
        rig, cmds = self.takeover(FAN3)
        rig.ac = HUMAN
        rig.polls(10)
        self.assertEqual(events(rig.poll()), [t.EV_TAKEOVER])
        self.assertEqual(rig.state, t.TAKEOVER)

    def test_clear_manual_stops_a_fan_run_at_once(self):
        rig, cmds = self.takeover(FAN3)
        cmds = rig.act(action="clear_manual")
        self.assertEqual(summary(cmds), [("event", t.EV_TAKEOVER), ("write", FAN3_OFF), ("persist",)])
        self.assertEqual(rig.state, t.STOPPING)

    def test_no_finish_fan(self):
        rig, cmds = self.takeover(HUMAN, finish_fan_min=0)
        self.assertEqual(writes(cmds), [t.ACState(False, "cool", 240, "2")])

    def test_human_change_during_takeover(self):
        rig, cmds = self.takeover(HUMAN)
        rig.readback()
        rig.ac = HUMAN
        self.assertEqual(events(rig.poll()), [t.EV_MANUAL])
        self.assertEqual(rig.state, t.MANUAL)


class TestUnreachable(unittest.TestCase):
    def test_event_on_third_miss(self):
        rig = Rig()
        rig.reachable = False
        self.assertEqual(rig.polls(2), [])
        self.assertEqual(events(rig.poll()), [t.EV_UNREACHABLE])
        self.assertEqual(rig.polls(10), [])
        self.assertEqual(rig.status()["misses"], 13)
        rig.reachable = True
        rig.poll()
        self.assertEqual(rig.status()["misses"], 0)
        rig.reachable = False
        self.assertEqual(events(rig.polls(3)), [t.EV_UNREACHABLE])

    def test_readback_does_not_count(self):
        rig = Rig()
        rig.reachable = False
        rig.polls(2)
        rig.readback()
        self.assertEqual(rig.status()["misses"], 2)

    def test_phase_moves_on_when_back(self):
        rig = Rig()
        rig.start_hot()
        rig.polls(20)
        rig.reachable = False
        self.assertEqual(events(rig.polls(15)), [t.EV_UNREACHABLE])  # cool phase ran out at minute 30
        self.assertEqual(rig.state, t.RUNNING)
        self.assertEqual(rig.snap["cycle"]["phase"], 0)
        rig.reachable = True
        cmds = rig.poll()
        self.assertEqual(summary(cmds), [("event", t.EV_PHASE), ("write", FAN3), ("persist",)])
        rig.readback()
        self.assertEqual(rig.polls(19), [])  # the fan phase runs in full
        self.assertEqual(writes(rig.poll()), [FAN3_OFF])

    def test_hard_cap_counts_through_gap(self):
        rig = Rig(hard_cap_min=60)
        rig.start_hot()
        rig.polls(30)
        rig.readback()
        rig.reachable = False
        rig.polls(40)
        rig.reachable = True
        cmds = rig.poll()
        self.assertEqual(summary(cmds), [("event", t.EV_HARD_CAP), ("write", FAN3_OFF), ("persist",)])

    def test_selector_resets(self):
        rig = Rig(rules=[rule("hot2", cond("temp", ">", 29), [phase("fan", 10)], confirm=2)])
        rig.temp = 300
        rig.poll()
        rig.reachable = False
        rig.poll()
        rig.reachable = True
        self.assertEqual(rig.poll(), [])
        self.assertEqual(events(rig.poll()), [t.EV_CYCLE_START])

    def test_off_streak_pauses_in_gap(self):
        rig = Rig()
        rig.ac = HUMAN
        rig.poll()
        rig.ac = OFF_AC
        rig.polls(11)  # 10 min counted
        rig.reachable = False
        rig.polls(20)
        rig.reachable = True
        rig.polls(20)  # the first read after the gap adds nothing: 19 more
        self.assertEqual(rig.state, t.MANUAL)
        rig.poll()
        self.assertEqual(rig.state, t.IDLE)

    def test_hold_over_waits_for_a_read(self):
        rig = Rig(manual_hold_min=10)
        rig.ac = HUMAN
        rig.poll()
        rig.polls(4)
        rig.reachable = False
        self.assertEqual(events(rig.polls(10)), [t.EV_UNREACHABLE])
        self.assertEqual(rig.state, t.MANUAL)
        rig.reachable = True
        self.assertEqual(events(rig.poll()), [t.EV_TAKEOVER])

    def test_no_writes_in_gap(self):
        rig = Rig()
        rig.start_hot()
        rig.reachable = False
        rig.temp = 250
        self.assertEqual(writes(rig.polls(60)), [])


class TestPause(unittest.TestCase):
    def test_idle_timed(self):
        rig = Rig()
        cmds = rig.act(action="pause", minutes=60)
        self.assertEqual(summary(cmds), [("event", t.EV_PAUSE), ("persist",)])
        self.assertEqual(rig.state, t.PAUSED)
        self.assertEqual(rig.snap["pause"], {"minutes": 60, "pending": False})
        rig.temp = 300
        rig.ac = HUMAN
        self.assertEqual(rig.polls(59), [])
        self.assertEqual(rig.status()["paused_left_s"], 60)
        cmds = rig.poll()
        self.assertEqual(summary(cmds), [("event", t.EV_RESUME), ("event", t.EV_MANUAL), ("persist",)])
        self.assertEqual(rig.state, t.MANUAL)  # still on: a fresh hold

    def test_until_resumed(self):
        rig = Rig()
        rig.act(action="pause", minutes=None)
        rig.polls(600)
        self.assertEqual(rig.state, t.PAUSED)
        self.assertIsNone(rig.status()["paused_left_s"])
        self.assertEqual(events(rig.act(action="resume")), [t.EV_RESUME])
        self.assertEqual(rig.state, t.IDLE)

    def test_finish_jumps_to_fan_then_paused(self):
        rig = Rig()
        rig.start_hot()
        rig.polls(5)
        cmds = rig.act(action="pause", minutes=60, cycle="finish")
        self.assertEqual(
            summary(cmds), [("event", t.EV_PAUSE), ("event", t.EV_PHASE), ("write", FAN3), ("persist",)]
        )
        self.assertEqual(rig.state, t.RUNNING)
        self.assertEqual(rig.snap["pause"], {"minutes": 60, "pending": True})
        self.assertTrue(rig.status()["pause_pending"])
        rig.readback()
        self.assertEqual(writes(rig.polls(20)), [FAN3_OFF])
        rig.readback()
        self.assertEqual(events(rig.poll()), [t.EV_CYCLE_END])
        self.assertEqual(rig.state, t.PAUSED)
        self.assertEqual(rig.polls(38), [])
        self.assertEqual(events(rig.poll()), [t.EV_RESUME])  # 60 min after the action

    def test_finish_in_fan_continues(self):
        rig = Rig()
        rig.start_hot()
        rig.temp = 260
        rig.poll()
        rig.readback()
        rig.polls(10)
        self.assertEqual(writes(rig.act(action="pause", minutes=60)), [])
        self.assertEqual(writes(rig.polls(10)), [FAN3_OFF])

    def test_finish_without_fan_phase(self):
        rig = Rig(finish_fan_min=15)
        rig.hum = 80
        rig.poll()
        cmds = rig.act(action="pause", minutes=60)
        self.assertEqual(writes(cmds), [t.ACState(True, "fan", None, "auto")])
        self.assertEqual(rig.snap["cycle"]["rule"]["phases"][1]["max_min"], 15)
        self.assertEqual(writes(rig.polls(15)), [t.ACState(False, "fan", None, "auto")])

    def test_finish_without_fan_and_no_finish_fan(self):
        rig = Rig(finish_fan_min=0)
        rig.hum = 80
        rig.poll()
        self.assertEqual(writes(rig.act(action="pause", minutes=60)), [t.ACState(False, "dry", None, "auto")])

    def test_leave(self):
        rig = Rig()
        rig.start_hot()
        cmds = rig.act(action="pause", minutes=60, cycle="leave")
        self.assertEqual(summary(cmds), [("event", t.EV_PAUSE), ("persist",)])
        self.assertEqual(rig.state, t.PAUSED)
        self.assertIsNone(rig.snap["cycle"])
        self.assertEqual(rig.ac, COOL)
        rig.ac = HUMAN
        self.assertEqual(rig.polls(30), [])
        self.assertEqual(events(rig.act(action="resume")), [t.EV_RESUME, t.EV_MANUAL])

    def test_stopping_completes_first(self):
        rig = Rig()
        rig.start_hot()
        rig.apply = False
        rig.act(action="stop_now")
        rig.act(action="pause", minutes=60, cycle="leave")
        self.assertEqual(rig.state, t.STOPPING)
        self.assertEqual(writes(rig.poll()), [OFF_AC])
        rig.apply = True
        rig.poll()
        rig.poll()
        self.assertEqual(rig.state, t.PAUSED)

    def test_second_pause_updates_timer(self):
        rig = Rig()
        rig.act(action="pause", minutes=60)
        rig.polls(50)
        rig.act(action="pause", minutes=120)
        rig.polls(119)
        self.assertEqual(rig.state, t.PAUSED)
        rig.poll()
        self.assertEqual(rig.state, t.IDLE)

    def test_resume_cancels_pending(self):
        rig = Rig()
        rig.start_hot()
        rig.act(action="pause", minutes=60)
        self.assertEqual(events(rig.act(action="resume")), [t.EV_RESUME])
        self.assertFalse(rig.status()["pause_pending"])
        rig.readback()
        rig.polls(21)
        self.assertEqual(rig.state, t.COOLDOWN)

    def test_human_during_finish_goes_paused(self):
        rig = Rig()
        rig.start_hot()
        rig.act(action="pause", minutes=60)
        rig.readback()
        rig.ac = HUMAN
        self.assertEqual(events(rig.poll()), [t.EV_MANUAL])
        self.assertEqual(rig.state, t.PAUSED)

    def test_from_manual(self):
        rig = Rig()
        rig.ac = HUMAN
        rig.poll()
        rig.act(action="pause", minutes=60)
        self.assertEqual(rig.state, t.PAUSED)
        self.assertEqual(events(rig.polls(60)), [t.EV_RESUME, t.EV_MANUAL])
        self.assertEqual(rig.state, t.MANUAL)

    def test_bad_minutes_ignored(self):
        rig = Rig()
        for bad in (0, 481, "60", 1.5, True):
            self.assertEqual(events(rig.act(action="pause", minutes=bad)), [t.EV_IGNORED])
        self.assertEqual(events(rig.act(action="pause", minutes=60, cycle="keep")), [t.EV_IGNORED])
        self.assertEqual(rig.state, t.IDLE)
        self.assertEqual(events(rig.act(action="resume")), [t.EV_IGNORED])


class TestProfileSwitch(unittest.TestCase):
    def test_new_selector(self):
        rig = Rig(rules=[rule("hot2", cond("temp", ">", 29), [phase("fan", 10)], confirm=2)])
        rig.profiles["profiles"]["other"]["rules"].append(rig.profiles["profiles"]["test"]["rules"][0])
        rig.temp = 300
        rig.poll()
        cmds = rig.poll([{"action": "set_profile", "profile": "other"}])
        self.assertEqual(summary(cmds), [("event", t.EV_PROFILE), ("persist",)])  # counting starts again
        self.assertEqual(rig.snap["profile"], "other")
        self.assertEqual(events(rig.poll()), [t.EV_CYCLE_START])

    def test_finish_then_idle(self):
        rig = Rig()
        rig.start_hot()
        cmds = rig.act(action="set_profile", profile="other", cycle="finish")
        self.assertEqual(events(cmds), [t.EV_PROFILE, t.EV_PHASE])
        self.assertEqual(writes(cmds), [FAN3])
        rig.readback()
        rig.polls(20)
        rig.readback()
        rig.poll()
        self.assertEqual(rig.state, t.IDLE)  # no cooldown: the new profile starts fresh

    def test_off_finish(self):
        rig = Rig()
        rig.start_hot()
        rig.act(action="set_profile", profile="off")
        rig.polls(22)
        self.assertEqual(rig.state, t.IDLE)
        self.assertEqual(rig.ac, FAN3_OFF)

    def test_leave_is_manual(self):
        rig = Rig()
        rig.start_hot()
        cmds = rig.act(action="set_profile", profile="other", cycle="leave")
        self.assertEqual(summary(cmds), [("event", t.EV_PROFILE), ("persist",)])
        self.assertEqual(rig.state, t.MANUAL)
        self.assertEqual(rig.ac, COOL)

    def test_leave_to_off_is_idle(self):
        rig = Rig()
        rig.start_hot()
        rig.act(action="set_profile", profile="off", cycle="leave")
        self.assertEqual(rig.state, t.IDLE)
        self.assertEqual(rig.polls(300), [])
        self.assertEqual(rig.ac, COOL)

    def test_manual_to_off_is_idle(self):
        rig = Rig()
        rig.ac = HUMAN
        rig.poll()
        rig.act(action="set_profile", profile="off")
        self.assertEqual(rig.state, t.IDLE)
        self.assertEqual(rig.polls(300), [])
        self.assertEqual(events(rig.act(action="set_profile", profile="test")), [t.EV_PROFILE, t.EV_MANUAL])

    def test_cooldown_to_idle(self):
        rig = Rig()
        rig.start_hot()
        rig.polls(51)
        self.assertEqual(rig.state, t.COOLDOWN)
        rig.act(action="set_profile", profile="other")
        self.assertEqual(rig.state, t.IDLE)

    def test_during_stopping(self):
        rig = Rig()
        rig.start_hot()
        rig.apply = False
        rig.act(action="stop_now")
        rig.act(action="set_profile", profile="other", cycle="leave")
        self.assertEqual(rig.state, t.STOPPING)
        rig.apply = True
        rig.polls(2)
        self.assertEqual(rig.state, t.IDLE)

    def test_paused_stays_paused(self):
        rig = Rig()
        rig.act(action="pause", minutes=None)
        rig.act(action="set_profile", profile="other")
        self.assertEqual(rig.state, t.PAUSED)

    def test_ignored(self):
        rig = Rig()
        for p in ("test", "nope", None, ["x"]):
            self.assertEqual(events(rig.act(action="set_profile", profile=p)), [t.EV_IGNORED])
        self.assertEqual(events(rig.act(action="set_profile", profile="off", cycle="x")), [t.EV_IGNORED])
        self.assertEqual(rig.ctl.active, "test")


class TestActions(unittest.TestCase):
    def test_run_from_idle(self):
        rig = Rig()
        cmds = rig.act(action="run", profile="test", rule=0)
        self.assertEqual(summary(cmds), [("event", t.EV_CYCLE_START), ("write", COOL), ("persist",)])

    def test_run_from_cooldown(self):
        rig = Rig()
        rig.start_hot()
        rig.polls(51)
        self.assertEqual(rig.state, t.COOLDOWN)
        self.assertEqual(writes(rig.act(action="run", profile="test", rule=1)), [DRY])

    def test_run_other_profile(self):
        rig = Rig()
        self.assertEqual(writes(rig.act(action="run", profile="other", rule=0)), [DRY])

    def test_run_skips_interval_and_counts_as_start(self):
        once = rule("once", cond("temp", ">", 29), [phase("fan", 5)], interval=60)
        rig = Rig(rules=[once])
        rig.act(action="run", profile="test", rule=0)
        self.assertEqual(rig.state, t.RUNNING)
        rig.polls(6)
        rig.temp = 300
        self.assertEqual(rig.polls(40), [])
        rig.act(action="run", profile="test", rule=0)
        self.assertEqual(rig.state, t.RUNNING)

    def test_run_ignored(self):
        rig = Rig()
        for bad in ({"profile": "test", "rule": 2}, {"profile": "nope", "rule": 0}, {"profile": "test"}):
            self.assertEqual(events(rig.act(action="run", **bad)), [t.EV_IGNORED])
        rig.reachable = False
        self.assertEqual(events(rig.act(action="run", profile="test", rule=0)), [t.EV_IGNORED])
        rig.reachable = True
        rig.start_hot()
        self.assertEqual(events(rig.act(action="run", profile="test", rule=1)), [t.EV_IGNORED])
        rig = Rig(active="off")
        rig.ac = HUMAN
        self.assertEqual(events(rig.act(action="run", profile="test", rule=0)), [t.EV_IGNORED])

    def test_stop_running_keeps_cooldown(self):
        rig = Rig()
        rig.start_hot()
        self.assertEqual(writes(rig.act(action="stop")), [FAN3])
        rig.readback()
        rig.polls(21)
        self.assertEqual(rig.state, t.COOLDOWN)

    def test_stop_in_takeover_continues(self):
        rig = Rig(manual_hold_min=10)
        rig.ac = HUMAN
        rig.polls(11)
        rig.readback()
        self.assertEqual(rig.state, t.TAKEOVER)
        self.assertEqual(rig.act(action="stop"), [])

    def test_stop_in_manual_takes_over(self):
        rig = Rig()
        rig.ac = HUMAN
        rig.poll()
        self.assertEqual(events(rig.act(action="stop")), [t.EV_TAKEOVER])
        self.assertEqual(rig.state, t.TAKEOVER)

    def test_stop_ignored(self):
        for state_rig in (Rig(), Rig(active="off")):
            self.assertEqual(events(state_rig.act(action="stop")), [t.EV_IGNORED])

    def test_stop_now(self):
        rig = Rig()
        rig.start_hot()
        self.assertEqual(summary(rig.act(action="stop_now")), [("write", OFF_AC), ("persist",)])
        rig.readback()
        rig.poll()
        self.assertEqual(rig.state, t.COOLDOWN)

    def test_stop_now_in_manual(self):
        rig = Rig()
        rig.ac = HUMAN
        rig.poll()
        cmds = rig.act(action="stop_now")
        human_off = t.ACState(False, "cool", 240, "2")
        self.assertEqual(summary(cmds), [("event", t.EV_TAKEOVER), ("write", human_off), ("persist",)])
        rig.readback()
        self.assertEqual(events(rig.poll()), [t.EV_CYCLE_END])
        self.assertEqual(rig.state, t.IDLE)

    def test_stop_now_in_manual_unreachable_checks_first(self):
        rig = Rig()
        rig.ac = HUMAN
        rig.poll()
        rig.reachable = False
        self.assertEqual(events(rig.act(action="stop_now")), [t.EV_TAKEOVER])
        rig.reachable = True
        rig.ac = t.ACState(True, "cool", 230, "2")  # changed meanwhile: the human wins
        self.assertEqual(events(rig.poll()), [t.EV_MANUAL])
        self.assertEqual(rig.state, t.MANUAL)

    def test_stop_now_ignored(self):
        rig = Rig()
        self.assertEqual(events(rig.act(action="stop_now")), [t.EV_IGNORED])
        rig.ac = HUMAN
        rig.poll()
        rig.ac = OFF_AC
        rig.poll()
        self.assertEqual(rig.state, t.MANUAL)
        self.assertEqual(events(rig.act(action="stop_now")), [t.EV_IGNORED])

    def test_clear_manual(self):
        rig = Rig()
        rig.ac = HUMAN
        rig.poll()
        self.assertEqual(events(rig.act(action="clear_manual")), [t.EV_TAKEOVER])
        rig = Rig()
        rig.ac = HUMAN
        rig.poll()
        rig.ac = OFF_AC
        rig.poll()
        self.assertEqual(summary(rig.act(action="clear_manual")), [("persist",)])
        self.assertEqual(rig.state, t.IDLE)
        self.assertEqual(events(rig.act(action="clear_manual")), [t.EV_IGNORED])

    def test_unknown_action(self):
        rig = Rig()
        self.assertEqual(events(rig.act(action="explode")), [t.EV_IGNORED])
        self.assertEqual(events(rig.step(1, ["pause"])), [t.EV_IGNORED])

    def test_held_over_readback(self):
        rig = Rig()
        rig.start_hot()
        rig.act(action="stop_now")
        self.assertEqual(summary(rig.step(5, [{"action": "pause", "minutes": 60}], readback=True)), [("persist",)])
        self.assertEqual(events(rig.poll()), [t.EV_PAUSE, t.EV_CYCLE_END])
        self.assertEqual(rig.state, t.PAUSED)


def snapshot_of(setup):
    # Run a rig into some state and return what state.json would hold.
    rig = Rig()
    setup(rig)
    return rig.snap, rig.ac


class TestBoot(unittest.TestCase):
    def boot(self, snap, ac, **kw):
        return Rig(snapshot=snap, ac=ac, start=time.ticks_add(0, 10 * MIN), **kw)

    def test_match_finishes_with_last_fan_phase(self):
        snap, ac = snapshot_of(lambda r: (r.start_hot(), r.polls(5)))
        rig = self.boot(snap, ac)
        cmds = rig.poll()
        self.assertEqual(summary(cmds), [("event", t.EV_PHASE), ("write", FAN3), ("persist",)])
        rig.readback()
        self.assertEqual(rig.polls(19), [])
        self.assertEqual(writes(rig.poll()), [FAN3_OFF])

    def test_fan_phase_restarts_in_full(self):
        def setup(r):
            r.start_hot()
            r.polls(40)  # 10 min into the fan phase

        snap, ac = snapshot_of(setup)
        rig = self.boot(snap, ac)
        self.assertEqual(writes(rig.poll()), [])  # already fan
        self.assertEqual(writes(rig.polls(19)), [])
        self.assertEqual(writes(rig.poll()), [FAN3_OFF])

    def test_no_fan_phase_uses_finish_fan(self):
        def setup(r):
            r.hum = 80
            r.poll()

        snap, ac = snapshot_of(setup)
        rig = self.boot(snap, ac, finish_fan_min=5)
        self.assertEqual(writes(rig.poll()), [t.ACState(True, "fan", None, "auto")])
        # finish_fan_min comes from the cycle's copy (20), not the settings at boot
        self.assertEqual(writes(rig.polls(20)), [t.ACState(False, "fan", None, "auto")])

    def test_unconfirmed_write_applied(self):
        def setup(r):
            r.temp = 300
            r.poll()  # written, never read back

        snap, ac = snapshot_of(setup)
        self.assertIsNotNone(snap["written"])
        rig = self.boot(snap, ac)
        self.assertEqual(writes(rig.poll()), [FAN3])

    def test_first_write_never_landed(self):
        def setup(r):
            r.temp = 300
            r.poll()

        snap, _ = snapshot_of(setup)
        rig = self.boot(snap, OFF_AC)  # still off: no fan for a coil that never got wet
        self.assertEqual(summary(rig.poll()), [("event", t.EV_CYCLE_END), ("persist",)])
        self.assertEqual(rig.state, t.COOLDOWN)

    def test_mismatch_is_manual_with_off_streak(self):
        snap, _ = snapshot_of(lambda r: r.start_hot())
        rig = self.boot(snap, OFF_AC)
        cmds = rig.poll()
        self.assertEqual(summary(cmds), [("event", t.EV_MANUAL), ("persist",)])
        self.assertEqual(rig.state, t.MANUAL)
        self.assertEqual(rig.polls(29), [])
        rig.poll()
        self.assertEqual(rig.state, t.IDLE)

    def test_mismatch_on(self):
        snap, _ = snapshot_of(lambda r: r.start_hot())
        rig = self.boot(snap, HUMAN, manual_hold_min=10)
        rig.poll()
        self.assertEqual(rig.state, t.MANUAL)
        rig.polls(10)
        self.assertEqual(rig.state, t.TAKEOVER)

    def test_mismatch_in_off_profile(self):
        snap, _ = snapshot_of(lambda r: r.start_hot())
        rig = self.boot(snap, HUMAN, active="off")
        self.assertEqual(events(rig.poll()), [t.EV_MANUAL])
        self.assertEqual(rig.state, t.IDLE)
        self.assertEqual(rig.polls(200), [])

    def test_stopping(self):
        def setup(r):
            r.start_hot()
            r.apply = False
            r.act(action="stop_now")

        snap, ac = snapshot_of(setup)
        self.assertEqual(snap["state"], t.STOPPING)
        rig = self.boot(snap, ac)
        self.assertEqual(writes(rig.poll()), [OFF_AC])
        rig = self.boot(snap, OFF_AC)
        self.assertEqual(events(rig.poll()), [t.EV_CYCLE_END])
        self.assertEqual(rig.state, t.COOLDOWN)

    def test_paused_gets_full_timer(self):
        snap, ac = snapshot_of(lambda r: (r.act(action="pause", minutes=60), r.polls(50)))
        rig = self.boot(snap, ac)
        rig.reachable = False
        rig.polls(5)
        rig.reachable = True
        rig.polls(60)  # the first read restarts the full 60 min
        self.assertEqual(rig.state, t.PAUSED)
        rig.poll()
        self.assertEqual(rig.state, t.IDLE)

    def test_pending_pause_survives(self):
        snap, ac = snapshot_of(lambda r: (r.start_hot(), r.act(action="pause", minutes=60)))
        self.assertTrue(snap["pause"]["pending"])
        rig = self.boot(snap, ac)
        rig.polls(22)
        self.assertEqual(rig.state, t.PAUSED)

    def test_cooldown_starts_fresh(self):
        snap, ac = snapshot_of(lambda r: (r.start_hot(), r.polls(70)))
        self.assertEqual(snap["state"], t.COOLDOWN)
        rig = self.boot(snap, ac)
        rig.polls(30)
        self.assertEqual(rig.state, t.COOLDOWN)
        rig.poll()
        self.assertEqual(rig.state, t.IDLE)

    def test_takeover_restarts_fan(self):
        def setup(r):
            r.settings["manual_hold_min"] = 10
            r.ac = HUMAN
            r.polls(15)

        snap, ac = snapshot_of(setup)
        self.assertEqual(snap["state"], t.TAKEOVER)
        rig = self.boot(snap, ac)
        self.assertEqual(events(rig.poll()), [t.EV_PHASE])
        self.assertEqual(writes(rig.polls(19)), [])
        self.assertEqual(writes(rig.poll()), [t.ACState(False, "fan", None, "2")])

    def test_waits_for_a_read(self):
        snap, ac = snapshot_of(lambda r: r.start_hot())
        rig = self.boot(snap, ac)
        self.assertTrue(rig.status()["booting"])
        rig.reachable = False
        self.assertEqual(rig.act(action="stop_now"), [])
        self.assertEqual(events(rig.polls(3)), [t.EV_UNREACHABLE])
        rig.reachable = True
        cmds = rig.poll()
        self.assertEqual(summary(cmds), [("event", t.EV_PHASE), ("write", OFF_AC), ("persist",)])
        self.assertFalse(rig.status()["booting"])

    def test_intervals_restart_in_full(self):
        damp = rule("damp", cond("hum", ">", 75), [phase("dry", 30)], interval=60)
        idle = {"v": 2, "state": "IDLE", "profile": "test", "cycle": None, "expected": None, "written": None,
                "pause": None, "intervals": ["damp"]}
        rig = self.boot(idle, OFF_AC, rules=[damp])
        rig.hum = 80
        self.assertEqual(rig.polls(60), [])  # the first poll decides the boot: the interval runs from there
        self.assertEqual(rig.ctl._snapshot(rig.now)["intervals"], ["damp"])
        self.assertEqual(events(rig.poll()), [t.EV_CYCLE_START])
        # A v1 snapshot has none; another profile's names are dropped.
        for snap in (dict(idle, v=1, intervals=None), dict(idle, profile="other")):
            rig = self.boot(snap, OFF_AC, rules=[damp])
            rig.hum = 80
            self.assertEqual(events(rig.poll()), [t.EV_CYCLE_START])

    def test_bad_snapshots(self):
        good, _ = snapshot_of(lambda r: r.start_hot())
        bad = [None, {}, {"v": 3, "state": "RUNNING"}, {"v": 1, "state": "NOPE"}, {"v": 1, "state": "RUNNING"}, []]
        bad.append(dict(good, intervals="damp"))
        broken = json.loads(json.dumps(good))
        broken["cycle"]["phase"] = 5
        bad.append(broken)
        broken = json.loads(json.dumps(good))
        broken["cycle"]["rule"]["phases"][0]["target_c"] = None
        bad.append(broken)
        broken = json.loads(json.dumps(good))
        broken["expected"] = [1, "cool"]
        bad.append(broken)
        for snap in bad:
            rig = self.boot(snap, HUMAN)
            self.assertEqual(rig.state, t.IDLE)
            self.assertEqual(events(rig.poll()), [t.EV_MANUAL])

    def test_idle_and_manual_follow_idle_rules(self):
        for setup in (lambda r: r.poll(), lambda r: (setattr(r, "ac", HUMAN), r.poll())):
            snap, _ = snapshot_of(setup)
            rig = self.boot(snap, HUMAN)
            self.assertEqual(events(rig.poll()), [t.EV_MANUAL])


class TestDryRun(unittest.TestCase):
    def test_writes_become_events(self):
        rig = Rig(dry_run=True)
        rig.temp = 300
        cmds = rig.poll()
        self.assertEqual(summary(cmds), [("event", t.EV_CYCLE_START), ("event", t.EV_WOULD), ("persist",)])
        self.assertEqual(rig.ac, OFF_AC)
        self.assertEqual(rig.polls(29), [])
        self.assertEqual(summary(rig.poll()), [("event", t.EV_PHASE), ("event", t.EV_WOULD), ("persist",)])
        self.assertEqual(rig.polls(19), [])
        self.assertEqual(summary(rig.poll()), [("event", t.EV_WOULD), ("persist",)])
        self.assertEqual(rig.state, t.STOPPING)
        self.assertEqual(summary(rig.poll()), [("event", t.EV_CYCLE_END), ("persist",)])
        self.assertEqual(rig.state, t.COOLDOWN)

    def test_remote_press_is_human(self):
        rig = Rig(dry_run=True)
        rig.temp = 300
        rig.polls(3)
        rig.ac = HUMAN
        self.assertEqual(events(rig.poll()), [t.EV_MANUAL])

    def test_messages_say_why_a_phase_ended(self):
        rig = Rig(dry_run=True)
        rig.temp = 300
        self.assertEqual(msgs(rig.poll()), ["hot: cool 26.0, fan auto", "would set cool 26.0, fan auto"])
        rig.polls(4)
        rig.temp = 260
        self.assertEqual(msgs(rig.poll()), ["phase 2/2: fan, fan 3 (goal temp <= 26)", "would set fan, fan 3"])
        rig.polls(19)
        self.assertEqual(msgs(rig.poll()), ["would set off (20 min)"])
        self.assertEqual(msgs(rig.poll()), ["hot ended"])

    def test_time_runs_out_before_the_goal(self):
        rig = Rig(dry_run=True)
        rig.temp = 300
        rig.polls(30)
        self.assertEqual(msgs(rig.poll()), ["phase 2/2: fan, fan 3 (30 min)", "would set fan, fan 3"])

    def test_stops_by_hand_have_no_reason(self):
        rig = Rig(dry_run=True, rules=[DAMP])
        rig.hum = 80
        rig.polls(3)
        self.assertEqual(msgs(rig.act(action="stop")), ["phase 2/2: fan, fan auto", "would set fan, fan auto"])
        self.assertEqual(msgs(rig.act(action="stop_now")), ["would set off"])

    def test_copied_at_start(self):
        rig = Rig(dry_run=True)
        rig.temp = 300
        rig.poll()
        rig.settings["dry_run"] = False
        self.assertEqual(writes(rig.polls(30)), [])
        self.assertEqual(rig.ac, OFF_AC)


class TestTicksWrap(unittest.TestCase):
    def trace(self, start):
        rig = Rig(start=start, manual_hold_min=10)
        out = []
        rig.temp = 300
        for i in range(140):
            if i == 1:
                rig.temp = 280
            if i == 100:
                rig.ac = HUMAN
            out.append(summary(rig.poll()))
            if i in (0, 31, 51, 111):
                out.append(summary(rig.readback()))
        return out

    def test_same_commands_across_wrap(self):
        plain = self.trace(0)
        self.assertEqual(self.trace(time.ticks_add(0, -45 * MIN)), plain)
        self.assertEqual(self.trace(time.ticks_add(0, -105 * MIN)), plain)
        flat = [cmd for cmds in plain for cmd in cmds]
        for ev in (t.EV_CYCLE_START, t.EV_PHASE, t.EV_CYCLE_END, t.EV_MANUAL, t.EV_TAKEOVER):
            self.assertIn(("event", ev), flat)


class TestStatus(unittest.TestCase):
    def test_running(self):
        rig = Rig()
        rig.start_hot()
        rig.polls(10)
        s = rig.status()
        self.assertEqual(s["state"], t.RUNNING)
        self.assertEqual(s["profile"], "test")
        cyc = s["cycle"]
        self.assertEqual((cyc["rule"], cyc["phase"], cyc["phases"], cyc["mode"]), ("hot", 1, 2, "cool"))
        self.assertEqual(cyc["phase_left_s"], 20 * 60 - 5)
        self.assertEqual(cyc["goal"], "temp <= 26")
        self.assertEqual(cyc["running_s"], 10 * 60 + 5)
        self.assertFalse(cyc["dry_run"])

    def test_idle_cooldown_manual(self):
        rig = Rig()
        s = rig.status()
        self.assertEqual((s["state"], s["cycle"], s["misses"], s["booting"]), (t.IDLE, None, 0, False))
        rig.start_hot()
        rig.polls(60)
        self.assertEqual(rig.status()["cooldown_left_s"], 21 * 60)
        rig.ac = HUMAN
        rig.poll()
        rig.polls(30)
        self.assertEqual(rig.status()["manual_left_s"], 90 * 60)


class TestPersist(unittest.TestCase):
    def test_only_on_change_and_json(self):
        rig = Rig()
        self.assertEqual(rig.polls(5), [])
        rig.start_hot()
        snap = rig.snap
        self.assertEqual(
            sorted(snap.keys()), ["cycle", "expected", "intervals", "pause", "profile", "state", "v", "written"]
        )
        self.assertEqual(snap["v"], 2)
        self.assertEqual(snap["cycle"]["kind"], "rule")
        self.assertEqual(snap["cycle"]["cooldown_min"], 30)
        self.assertEqual(snap["cycle"]["hard_cap_min"], 120)
        self.assertEqual(rig.polls(5), [])
