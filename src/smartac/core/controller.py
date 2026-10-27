# SPDX-License-Identifier: GPL-3.0-only
# The state machine (docs/idea.md, "State machine"; ownership: ADR 0003; time: ADR 0004).
# step(now, reading, actions) -> [(CMD_WRITE | CMD_PERSIST | CMD_EVENT, payload)]. Pure: the shell does the
# reads, writes and files; time is passed in as monotonic ms and only compared through ticks_since.
#
# One step: prune the selector -> reachability and miss count -> ownership check (may go MANUAL) -> actions ->
# timers, goals and selection -> at most one write -> persist if the snapshot changed.
# A read-back step (readback=True, ~5 s after a write) only confirms the write or counts a failed attempt.
import json

from smartac.core.config import OFF
from smartac.core.rules import RuleSelector, goal_met
from smartac.core.types import (
    ACT_CLEAR_MANUAL,
    ACT_PAUSE,
    ACT_RESUME,
    ACT_RUN,
    ACT_SET_PROFILE,
    ACT_STOP,
    ACT_STOP_NOW,
    CMD_EVENT,
    CMD_PERSIST,
    CMD_WRITE,
    COOLDOWN,
    EV_CYCLE_END,
    EV_CYCLE_START,
    EV_HARD_CAP,
    EV_IGNORED,
    EV_MANUAL,
    EV_PAUSE,
    EV_PHASE,
    EV_PROFILE,
    EV_RESUME,
    EV_TAKEOVER,
    EV_UNREACHABLE,
    EV_WOULD,
    EV_WRITE_FAILED,
    FAN_AUTO,
    IDLE,
    MANUAL,
    MODE_COOL,
    MODE_DRY,
    MODE_FAN,
    PAUSED,
    RUNNING,
    STOPPING,
    TAKEOVER,
    ACState,
    tenths,
    ticks_since,
)

SNAPSHOT_VERSION = 2  # 2 added "intervals"; a v1 snapshot is read as having none
MIN_MS = 60 * 1000
UNREACHABLE_MISSES = 3  # consecutive failed poll reads before EV_UNREACHABLE
MAX_PAUSE_MIN = 480  # longest timed pause; "until resumed" has no timer
FINISH = "finish"
LEAVE = "leave"
KIND_RULE = "rule"
KIND_TAKEOVER = "takeover"

STATES = (IDLE, RUNNING, STOPPING, COOLDOWN, MANUAL, TAKEOVER, PAUSED)
CYCLING = (RUNNING, TAKEOVER)  # a phase runs
OWNED = (RUNNING, TAKEOVER, STOPPING)  # we own the AC
WET = (MODE_COOL, MODE_DRY)  # modes that leave the coil wet
CYCLE_KEYS = ("kind", "rule", "phase", "cooldown_min", "hard_cap_min", "dry_run", "finish_fan_min")


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _same(ac, ref):
    # Does the AC read as `ref`? An off state is checked on power only: the AC keeps its mode when off.
    if ac is None or ref is None:
        return False
    if not ref.power:
        return not ac.power
    return ac == ref


def _off(st):
    return ACState(False, st.mode, st.target, st.fan)


def _phase_state(phase):
    mode = phase["mode"]
    return ACState(True, mode, tenths(phase["target_c"]) if mode == MODE_COOL else None, phase["fan"])


def _fan_phase(fan, minutes):
    return {"mode": MODE_FAN, "fan": fan, "max_min": minutes, "until": None}


def _phase_ok(phase):
    # A restored phase must be one we can turn into a write.
    mode = phase["mode"]
    if mode not in (MODE_COOL, MODE_DRY, MODE_FAN) or not isinstance(phase["fan"], str):
        return False
    if mode == MODE_COOL:
        tenths(phase["target_c"])  # raises TypeError on a missing or non-numeric target
    return _is_int(phase["max_min"])


def _ac_list(st):
    return None if st is None else [st.power, st.mode, st.target, st.fan]


def _ac_from(v):
    if v is None:
        return None
    power, mode, target, fan = v
    if not isinstance(power, bool) or not isinstance(mode, str):
        raise ValueError("bad AC state")
    return ACState(power, mode, target, fan)


def _describe(st):
    if not st.power:
        return "off"
    s = st.mode
    if st.target is not None:
        s += " %d.%d" % (st.target // 10, st.target % 10)
    if st.fan is not None:
        s += ", fan " + st.fan
    return s


def _goal_text(until):
    if until is None:
        return None
    return "%s %s %s" % (until.get("sensor"), until.get("op"), until.get("value"))


def _secs(ms):
    # Milliseconds left -> whole seconds, rounded up so "0" means done.
    return (ms + 999) // 1000 if ms > 0 else 0


class Controller:
    def __init__(self, profiles, settings, now, snapshot=None):
        # now is the boot time; snapshot is the parsed state.json, or None on a fresh start.
        self.profiles = profiles
        self.settings = settings
        self.active = profiles.get("active", OFF)
        self.selector = RuleSelector()
        self.state = IDLE
        # The cycle we run (RUNNING, TAKEOVER, STOPPING) or whose cooldown runs (COOLDOWN). Keys: CYCLE_KEYS.
        # "rule" is a copy taken at the start; it is replaced, never changed in place (snapshots share it).
        self.cycle = None
        self.cycle_start = None
        self.phase_start = None  # None until the phase's state has been written
        self.cooldown_start = None
        # Ownership (ADR 0003). expected: the confirmed state we set (in dry_run: the real state when the run
        # began, since nothing is written). written: sent, not yet confirmed. sent: the last state we wrote, or
        # would have. seen: the last reachable AC state. fails: failed attempts of `written`; judged: this
        # attempt has been counted.
        self.expected = None
        self.written = None
        self.sent = None
        self.seen = None
        self.fails = 0
        self.judged = False
        # MANUAL: hold timer, "hold over" by request, and the off streak (ms of reachable reads, AC off).
        self.hold_start = None
        self.hold_over = False
        self.off_ms = 0
        self.off_at = None
        # {"minutes": int | None, "since": ms | None, "pending": bool}; pending: waits for the cycle to end.
        self.pause = None
        self.misses = 0
        self.queued = []  # actions held over a read-back step or until boot is decided
        self.why = None  # why the last phase ended, for the off write's event; messages only, not persisted
        self.intervals = []  # restored rule names whose min_interval restarts when boot is decided
        self.booting = snapshot is not None
        if snapshot is not None:
            self._restore(snapshot)
        self._persisted = self._snapshot(now)

    def configure(self, profiles=None, settings=None):
        # New config documents. A running cycle keeps the copies it took at its start.
        if profiles is not None:
            self.profiles = profiles
        if settings is not None:
            self.settings = settings

    def step(self, now, reading, actions=(), readback=False):
        out = []
        self.selector.prune(now)
        ac = reading.ac
        if readback:
            if ac is not None and not self.booting and self.state in OWNED and self.written is not None:
                self._judge(now, ac, out, True)
            self.queued.extend(actions)
            self._persist(now, out)
            return out

        if ac is None:
            self.misses += 1
            if self.misses == UNREACHABLE_MISSES:
                self._event(out, EV_UNREACHABLE, "AC not reachable for %d reads" % self.misses)
            self.off_at = None  # a gap neither extends nor breaks the MANUAL off streak
        else:
            self.misses = 0

        if self.booting:
            if ac is None:
                self.queued.extend(actions)
                return out
            self.booting = False
            self._resolve_boot(now, ac, out)
        elif ac is not None:
            self._check(now, ac, out)
        if ac is not None:
            self.seen = ac

        actions = self.queued + list(actions)
        self.queued = []
        for action in actions:
            self._act(now, reading, action, out)
        self._advance(now, reading, out)
        if ac is not None and self.state in OWNED:
            self._write(now, out)
        self._persist(now, out)
        return out

    def status(self, now):
        # The controller's part of /api/state. Times are seconds left (or running), never clock times.
        st = self.state
        s = {
            "state": st,
            "profile": self.active,
            "cycle": None,
            "cooldown_left_s": None,
            "manual_left_s": None,
            "paused_left_s": None,
            "pause_pending": self.pause is not None and self.pause["pending"],
            "misses": self.misses,
            "booting": self.booting,
        }
        if self.booting:
            return s  # no timers run until the first reachable read
        c = self.cycle
        if st in OWNED:
            phases = c["rule"]["phases"]
            info = {
                "rule": c["rule"]["name"],
                "kind": c["kind"],
                "phase": None,
                "phases": len(phases),
                "mode": None,
                "phase_left_s": None,
                "goal": None,
                "running_s": ticks_since(now, self.cycle_start) // 1000,
                "dry_run": c["dry_run"],
            }
            if st in CYCLING:
                phase = phases[c["phase"]]
                left = phase["max_min"] * MIN_MS
                if self.phase_start is not None:
                    left -= ticks_since(now, self.phase_start)
                info["phase"] = c["phase"] + 1
                info["mode"] = phase["mode"]
                info["phase_left_s"] = _secs(left)
                info["goal"] = _goal_text(phase.get("until"))
            s["cycle"] = info
        elif st == COOLDOWN:
            s["cooldown_left_s"] = _secs(c["cooldown_min"] * MIN_MS - ticks_since(now, self.cooldown_start))
        elif st == MANUAL:
            left = self.settings["manual_hold_min"] * MIN_MS - ticks_since(now, self.hold_start)
            if self.hold_over:
                s["manual_left_s"] = 0
            elif left <= 0 and self.seen is not None and self.seen.mode == MODE_FAN:
                s["manual_left_s"] = None  # a fan run by hand: no takeover
            else:
                s["manual_left_s"] = _secs(left)
        elif st == PAUSED and self.pause["minutes"] is not None:
            s["paused_left_s"] = _secs(self.pause["minutes"] * MIN_MS - ticks_since(now, self.pause["since"]))
        return s

    # --- ownership ---

    def _check(self, now, ac, out):
        # A reachable poll read, before any action or write.
        st = self.state
        if st in OWNED:
            if st == STOPPING and not ac.power and not self.cycle["dry_run"]:
                # Off is what we want, whoever switched it off.
                self.expected = self.sent = _off(self.sent)
                self.written = None
            elif self.written is not None:
                self._judge(now, ac, out, False)
            elif not _same(ac, self.expected):
                self._manual(now, ac, out, EV_MANUAL, "AC changed by hand")
        elif st == MANUAL:
            if ac != self.seen:
                self.hold_start = now  # a new change by the human restarts the hold
                self.hold_over = False
            if ac.power:
                self.off_ms = 0
                self.off_at = None
            else:
                if self.off_at is not None:
                    self.off_ms += ticks_since(now, self.off_at)
                self.off_at = now

    def _judge(self, now, ac, out, readback):
        # A read after a write: confirmed, a failed attempt (nothing applied), or a human.
        if _same(ac, self.written):
            self.expected = self.written
            self.written = None
            self.fails = 0
            self.judged = False
            return
        if not readback and ac != self.seen:
            self._manual(now, ac, out, EV_MANUAL, "AC changed by hand")
            return
        # On a read-back any mismatch counts as a failed attempt: the AC may still be applying the write.
        if self.judged:
            return
        self.judged = True
        self.fails += 1
        if self.fails < 2:
            return  # retried on the next poll step
        if self.state == STOPPING:
            if self.fails == 2:
                self._event(out, EV_WRITE_FAILED, "off not applied; retrying every poll")
        else:
            self._manual(now, ac, out, EV_WRITE_FAILED, "%s not applied twice" % _describe(self.written))

    def _own(self, ac):
        self.expected = ac
        self.sent = ac
        self.written = None
        self.fails = 0
        self.judged = False

    def _release(self):
        # Give up the AC and the cycle.
        self.why = None
        self.cycle = None
        self.cycle_start = None
        self.phase_start = None
        self.expected = None
        self.written = None
        self.sent = None
        self.fails = 0
        self.judged = False

    # --- transitions ---

    def _manual(self, now, ac, out, kind, msg):
        # Hands off. A pending pause wins; the off profile has no MANUAL.
        if kind is not None:
            self._event(out, kind, msg)
        self._release()
        if self.pause is not None and self.pause["pending"]:
            self.pause["pending"] = False
            self.state = PAUSED
        elif self.active == OFF:
            self.state = IDLE
        else:
            self.state = MANUAL
            self.hold_start = now
            self.hold_over = False
            self.off_ms = 0
            self.off_at = now if ac is not None and not ac.power else None

    def _idle(self):
        self._release()
        self.state = IDLE

    def _start(self, now, rule, ac, out):
        st = self.settings
        self._own(ac)
        self.cycle = {
            "kind": KIND_RULE,
            "rule": json.loads(json.dumps(rule)),
            "phase": 0,
            "cooldown_min": rule.get("cooldown_min", 0),
            "hard_cap_min": st["hard_cap_min"],
            "dry_run": st["dry_run"],
            "finish_fan_min": st["finish_fan_min"],
        }
        self.cycle_start = now
        self.phase_start = None
        self.state = RUNNING
        self._event(out, EV_CYCLE_START, "%s: %s" % (rule["name"], _describe(_phase_state(rule["phases"][0]))))

    def _takeover(self, now, ac, out, msg, fan=True):
        # Own a human's run and end it like a cycle: fan if it was cooling or drying, then off.
        st = self.settings
        phases = []
        if fan and ac.mode in WET and st["finish_fan_min"] > 0:
            phases.append(_fan_phase(ac.fan or FAN_AUTO, st["finish_fan_min"]))
        self._event(out, EV_TAKEOVER, msg)
        self._own(ac)
        self.cycle = {
            "kind": KIND_TAKEOVER,
            "rule": {"name": None, "phases": phases},
            "phase": 0,
            "cooldown_min": 0,
            "hard_cap_min": st["hard_cap_min"],
            "dry_run": st["dry_run"],
            "finish_fan_min": st["finish_fan_min"],
        }
        self.cycle_start = now
        self.phase_start = None
        self.state = TAKEOVER if phases else STOPPING

    def _set_phases(self, phases):
        rule = dict(self.cycle["rule"])
        rule["phases"] = phases
        self.cycle["rule"] = rule

    def _goto(self, i, out, why=None):
        c = self.cycle
        c["phase"] = i
        self.phase_start = None
        phases = c["rule"]["phases"]
        msg = "phase %d/%d: %s" % (i + 1, len(phases), _describe(_phase_state(phases[i])))
        self._event(out, EV_PHASE, msg if why is None else "%s (%s)" % (msg, why))

    def _next_phase(self, out, why):
        # why: what ended the phase ("goal hum <= 65" or "45 min"), shown on the next phase or the off write.
        c = self.cycle
        if c["phase"] + 1 < len(c["rule"]["phases"]):
            self._goto(c["phase"] + 1, out, why)
        else:
            self.why = why
            self.state = STOPPING

    def _finish(self, out, restart=False):
        # End the running cycle now: a fan phase to dry the coil, then off (pause/profile "finish", Stop, reboot).
        # A fan phase that already runs continues; otherwise the rule's last fan phase runs in full, or
        # finish_fan_min of fan after cool/dry. restart (reboot): elapsed time is unknown, so phases restart.
        c = self.cycle
        phases = c["rule"]["phases"]
        i = c["phase"]
        cur = phases[i]
        last_fan = phases[-1]["mode"] == MODE_FAN
        if cur["mode"] == MODE_FAN and not (restart and last_fan):
            self._set_phases(phases[: i + 1])
            if restart:
                self.phase_start = None
        elif last_fan:
            self._goto(len(phases) - 1, out)
        elif cur["mode"] in WET and c["finish_fan_min"] > 0:
            self._set_phases(phases[: i + 1] + [_fan_phase(cur["fan"], c["finish_fan_min"])])
            self._goto(i + 1, out)
        else:
            self.state = STOPPING

    def _end(self, now, out):
        # Off is confirmed: the cycle is over.
        c = self.cycle
        self._event(out, EV_CYCLE_END, "%s ended" % (c["rule"]["name"] or c["kind"]))
        self._release()
        if self.pause is not None and self.pause["pending"]:
            self.pause["pending"] = False
            self.state = PAUSED
        elif c["cooldown_min"] > 0:
            self.cycle = c
            self.cooldown_start = now
            self.state = COOLDOWN
        else:
            self.state = IDLE

    def _resolve_boot(self, now, ac, out):
        # The first reachable read after a restart decides what the restored state means (ADR 0003, 0004).
        st = self.state
        self.selector.restart(self.intervals, now)
        self.intervals = []
        if self.pause is not None and self.pause["minutes"] is not None:
            self.pause["since"] = now  # the full pause again
        if st == COOLDOWN:
            self.cooldown_start = now
        elif st in OWNED:
            c = self.cycle
            if not c["dry_run"]:
                if _same(ac, self.written):
                    self.expected = self.written
                elif st == STOPPING and not ac.power:
                    self.expected = _off(self.expected)
            self.written = None
            if not _same(ac, self.expected):
                self._manual(now, ac, out, EV_MANUAL, "AC changed while the controller was down")
                return
            self._own(self.expected)
            self.cycle_start = now
            self.phase_start = None
            if st in CYCLING:
                if not ac.power and not c["dry_run"]:
                    self.state = STOPPING  # still off (the first write never landed): nothing to finish
                else:
                    self._finish(out, restart=True)  # a cool/dry phase is never resumed

    def _advance(self, now, reading, out):
        # Timers, goals and rule selection, in an order that lets one step chain (e.g. COOLDOWN -> IDLE -> RUNNING).
        ac = reading.ac
        st = self.settings
        if self.state == PAUSED:
            p = self.pause
            if p["minutes"] is not None and ticks_since(now, p["since"]) >= p["minutes"] * MIN_MS:
                self.pause = None
                self._idle()
                self._event(out, EV_RESUME, "pause over")

        if self.state == COOLDOWN:
            if ac is not None and ac.power and self.active != OFF:
                self._manual(now, ac, out, EV_MANUAL, "AC on during cooldown")
            elif ticks_since(now, self.cooldown_start) >= self.cycle["cooldown_min"] * MIN_MS:
                self._idle()

        if self.state == MANUAL and ac is not None:
            if not ac.power and self.off_ms >= st["manual_release_off_min"] * MIN_MS:
                self._idle()
            elif self.hold_over or ticks_since(now, self.hold_start) >= st["manual_hold_min"] * MIN_MS:
                if not ac.power:
                    self._idle()
                elif self.hold_over or ac.mode != MODE_FAN:
                    self._takeover(now, ac, out, "manual hold over")
                # else: a fan run by hand is left alone (it dries the coil) until it is switched off.

        if self.state == IDLE and self.active != OFF:
            rules = self._rules()
            if ac is not None and ac.power:
                self._manual(now, ac, out, EV_MANUAL, "AC on, not started by us")
            else:
                i = self.selector.observe(rules, reading, now)
                if i is not None:
                    self.selector.started(rules[i], now)
                    self._start(now, rules[i], ac, out)

        if self.state in CYCLING and ac is not None:
            c = self.cycle
            if ticks_since(now, self.cycle_start) >= c["hard_cap_min"] * MIN_MS:
                self._event(out, EV_HARD_CAP, "%d min cap reached" % c["hard_cap_min"])
                self.state = STOPPING
            elif self.phase_start is not None:
                phase = c["rule"]["phases"][c["phase"]]
                until = phase.get("until")
                if goal_met(until, reading):
                    self._next_phase(out, "goal " + _goal_text(until))
                elif ticks_since(now, self.phase_start) >= phase["max_min"] * MIN_MS:
                    self._next_phase(out, "%d min" % phase["max_min"])

        if self.state == STOPPING and self.written is None and not self.sent.power:
            self._end(now, out)

    def _write(self, now, out):
        # At most one write per step, decided from this step's read. A failed attempt is sent again.
        c = self.cycle
        if self.state == STOPPING:
            want = _off(self.sent)
        else:
            want = _phase_state(c["rule"]["phases"][c["phase"]])
            if self.phase_start is None:
                self.phase_start = now
        if self.written is None and _same(self.sent, want):
            return
        if self.written != want:
            self.fails = 0
        self.sent = want
        self.judged = False
        why, self.why = self.why, None
        if c["dry_run"]:
            msg = "would set " + _describe(want)
            self._event(out, EV_WOULD, msg if why is None else "%s (%s)" % (msg, why))
        else:
            self.written = want
            out.append((CMD_WRITE, want))

    # --- actions ---

    def _act(self, now, reading, action, out):
        name = action.get("action") if isinstance(action, dict) else None
        state = self.state
        if name == ACT_PAUSE:
            ok = self._pause(now, action, out)
        elif name == ACT_RESUME:
            ok = self._resume(out)
        elif name == ACT_RUN:
            ok = self._run(now, action, reading.ac, out)
        elif name == ACT_STOP:
            ok = self._stop(out)
        elif name == ACT_STOP_NOW:
            ok = self._stop_now(now, out)
        elif name == ACT_CLEAR_MANUAL:
            ok = self._clear_manual()
        elif name == ACT_SET_PROFILE:
            ok = self._set_profile(now, action, out)
        else:
            ok = False
        if not ok:
            self._event(out, EV_IGNORED, "%s ignored in %s" % (name, state))

    def _pause(self, now, action, out):
        minutes = action.get("minutes")
        choice = action.get("cycle") or FINISH
        if minutes is not None and (not _is_int(minutes) or not 1 <= minutes <= MAX_PAUSE_MIN):
            return False
        if choice not in (FINISH, LEAVE):
            return False
        self.pause = {"minutes": minutes, "since": None if minutes is None else now, "pending": False}
        self._event(out, EV_PAUSE, "until resumed" if minutes is None else "%d min" % minutes)
        if self.state in CYCLING and choice == FINISH:
            self.pause["pending"] = True
            self._finish(out)
        elif self.state == STOPPING:
            self.pause["pending"] = True
        else:
            self._release()  # "leave": ownership dropped; a hold or cooldown is forgotten
            self.state = PAUSED
        return True

    def _resume(self, out):
        if self.state == PAUSED:
            self.pause = None
            self._idle()
        elif self.pause is not None and self.pause["pending"]:
            self.pause = None  # the cycle still finishes, then cools down as usual
        else:
            return False
        self._event(out, EV_RESUME, "resumed")
        return True

    def _run(self, now, action, ac, out):
        # Run now: start a rule at once, skipping its trigger and min_interval.
        if self.state not in (IDLE, COOLDOWN) or ac is None or ac.power:
            return False
        name = action.get("profile")
        profile = self.profiles.get("profiles", {}).get(name) if isinstance(name, str) else None
        rules = profile.get("rules") if isinstance(profile, dict) else None
        i = action.get("rule")
        if not isinstance(rules, list) or not _is_int(i) or not 0 <= i < len(rules):
            return False
        if name == self.active:
            self.selector.started(rules[i], now)
        self._start(now, rules[i], ac, out)
        return True

    def _stop(self, out):
        # Stop: finish with a fan phase, then off. In MANUAL: take over now.
        if self.state == RUNNING:
            self._finish(out)
        elif self.state == MANUAL:
            self.hold_over = True
        elif self.state != TAKEOVER:  # a takeover already is the finish
            return False
        return True

    def _clear_manual(self):
        # Treated as "hold over": IDLE or TAKEOVER on the next reachable read (this step's, if it was one).
        if self.state != MANUAL:
            return False
        self.hold_over = True
        return True

    def _stop_now(self, now, out):
        # Stop now: off at once, no fan phase.
        if self.state in CYCLING:
            self.state = STOPPING
        elif self.state == MANUAL and self.seen is not None and self.seen.power:
            # Checked against the next read like any owned state, so a change in between still wins.
            self._takeover(now, self.seen, out, "stop now", fan=False)
        else:
            return False
        return True

    def _set_profile(self, now, action, out):
        name = action.get("profile")
        choice = action.get("cycle") or FINISH
        if not isinstance(name, str) or name == self.active:
            return False
        if name != OFF and name not in self.profiles.get("profiles", {}):
            return False
        if choice not in (FINISH, LEAVE):
            return False
        self.active = name
        self.selector = RuleSelector()
        self._event(out, EV_PROFILE, "profile " + name)
        st = self.state
        if st in CYCLING and choice == LEAVE:
            self._manual(now, self.seen, out, None, None)  # the run becomes a human's
        elif st in OWNED:
            self.cycle["cooldown_min"] = 0  # finish, then the new profile starts from IDLE
            if st in CYCLING:
                self._finish(out)
        elif st == COOLDOWN or (st == MANUAL and name == OFF):
            self._idle()
        return True

    # --- helpers ---

    def _rules(self):
        profile = self.profiles.get("profiles", {}).get(self.active)
        return profile.get("rules", []) if isinstance(profile, dict) else []

    def _event(self, out, kind, msg):
        out.append((CMD_EVENT, (kind, msg)))

    def _snapshot(self, now):
        # What state.json holds: no stamps (elapsed time is unknown after a restart, ADR 0004). "intervals": the
        # active profile's rules still inside min_interval_min, restarted in full after a reboot; it changes when
        # a cycle starts and when an interval runs out.
        cycle = None
        if self.cycle is not None and self.state in OWNED + (COOLDOWN,):
            cycle = dict(self.cycle)
        pause = None
        if self.pause is not None:
            pause = {"minutes": self.pause["minutes"], "pending": self.pause["pending"]}
        return {
            "v": SNAPSHOT_VERSION,
            "state": self.state,
            "profile": self.active,
            "cycle": cycle,
            "expected": _ac_list(self.expected),
            "written": _ac_list(self.written),
            "pause": pause,
            "intervals": self.intervals if self.booting else self.selector.within(self._rules(), now),
        }

    def _persist(self, now, out):
        snap = self._snapshot(now)
        if snap != self._persisted:
            self._persisted = snap
            out.append((CMD_PERSIST, snap))

    def _restore(self, snap):
        # A snapshot that doesn't parse is treated as none: the AC is judged by the IDLE rules.
        # The active profile comes from profiles.json; the snapshot's "profile" is informational.
        try:
            state = snap["state"]
            if snap.get("v") not in (1, SNAPSHOT_VERSION) or state not in STATES:
                return
            intervals = snap.get("intervals") or []
            if not isinstance(intervals, list) or not all(isinstance(n, str) for n in intervals):
                return
            cycle = expected = written = pause = None
            if state in OWNED or state == COOLDOWN:
                cycle = {k: snap["cycle"][k] for k in CYCLE_KEYS}
                phases = cycle["rule"]["phases"]
                if "name" not in cycle["rule"]:
                    return
                for k in ("phase", "cooldown_min", "hard_cap_min", "finish_fan_min"):
                    if not _is_int(cycle[k]):
                        return
                if not isinstance(phases, list) or not all(_phase_ok(ph) for ph in phases):
                    return
                if state in CYCLING and not 0 <= cycle["phase"] < len(phases):
                    return
            if state in OWNED:
                expected = _ac_from(snap["expected"])
                written = _ac_from(snap.get("written"))
                if expected is None:
                    return
            p = snap.get("pause")
            if p is not None and (state == PAUSED or (state in OWNED and p["pending"])):
                minutes = p["minutes"]
                if minutes is not None and (not _is_int(minutes) or not 1 <= minutes <= MAX_PAUSE_MIN):
                    return
                pause = {"minutes": minutes, "since": None, "pending": state != PAUSED}
            if state == PAUSED and pause is None:
                return
        except (AttributeError, IndexError, KeyError, TypeError, ValueError):
            return
        if state == MANUAL:
            state = IDLE  # a fresh hold starts if the AC is still on
        self.state = state
        self.cycle = cycle
        self.expected = expected
        self.written = written
        self.pause = pause
        if snap.get("profile") == self.active:  # names of another profile's rules mean nothing here
            self.intervals = intervals
