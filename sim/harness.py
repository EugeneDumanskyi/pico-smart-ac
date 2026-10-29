# SPDX-License-Identifier: GPL-3.0-only
# The month run (roadmap M2): the whole app (shell/app.py) against the simulator over HTTP on 127.0.0.1, in
# virtual time, with a scenario of remote presses, outages, failing writes, user actions and power cuts
# (sim/scenario.py). A Monitor checks invariants after every controller step and at every scheduler wakeup; the
# first violation stops the run.
#
#   micropython sim/harness.py [--days 30] [--seed 1] [--port 8765] [--quiet] [--show]
#
# Virtual time: a FakeClock is the world clock and the board's sleep hook is the scheduler. Only the control task
# sleeps, so each sleep moves the clock to the deadline or the next timeline entry, whichever is first; no time
# passes during HTTP. A seed replays a run exactly. Each boot gets its own ticks offset, starting 0-3 days
# before the ticks wrap (the Unix port's ticks period is 2^62, so a run would never wrap by itself).
import sys

if __name__ == "__main__":
    _ROOT = (__file__.rsplit("/", 1)[0] if "/" in __file__ else ".") + "/.."
    sys.path.insert(0, _ROOT + "/src")
    sys.path.insert(0, _ROOT)

import asyncio  # noqa: E402
import time  # noqa: E402

from sim import scenario  # noqa: E402
from sim.clock import MIN_MS, FakeClock  # noqa: E402
from sim.device import FakeDaikin  # noqa: E402
from sim.server import DEFAULT_PORT, serve  # noqa: E402
from smartac.board.host import HostBoard  # noqa: E402
from smartac.core import clock as wallclock  # noqa: E402
from smartac.core import config  # noqa: E402
from smartac.core.config import OFF  # noqa: E402
from smartac.core.controller import CYCLING, OWNED  # noqa: E402
from smartac.core.types import (  # noqa: E402
    ACT_CLEAR_MANUAL,
    ACT_PAUSE,
    ACT_RESUME,
    ACT_SET_PROFILE,
    ACT_STOP,
    ACT_STOP_NOW,
    CMD_EVENT,
    CMD_WRITE,
    COOLDOWN,
    EV_CYCLE_END,
    EV_CYCLE_START,
    EV_HARD_CAP,
    EV_MANUAL,
    EV_STAGE,
    EV_TAKEOVER,
    EV_UNREACHABLE,
    EV_WRITE_FAILED,
    IDLE,
    MANUAL,
    MODE_FAN,
    PAUSED,
    STOPPING,
)
from smartac.shell.app import SETTLE_MS, App  # noqa: E402
from smartac.shell.daikin import DaikinClient  # noqa: E402
from smartac.shell.store import MemoryStore  # noqa: E402

DAY_MS = 24 * 60 * MIN_MS
RECENT = 12  # steps shown with a violation
# Counts a full run must reach, so the scenario really exercised them.
COVERAGE = (
    "cycles",
    "holds",
    "takeovers",
    "write_failed",
    "hard_caps",
    "stop_retries",
    "reboots_mid_cycle",
    "reboots_mid_write",
    "outages",
    "wraps",
    "stages",
)
MANUAL_EXITS = (ACT_STOP, ACT_STOP_NOW, ACT_CLEAR_MANUAL, ACT_PAUSE, ACT_SET_PROFILE)
HANDOFFS = (EV_CYCLE_END, EV_MANUAL, EV_WRITE_FAILED)


class Reboot(Exception):
    pass


class Finished(Exception):
    pass


class Violation(Exception):
    pass


def _same(ac, ref):
    # As the controller compares: off is checked on power only.
    if ac is None or ref is None:
        return False
    if not ref.power:
        return not ac.power
    return ac == ref


def _clock(t):
    s = t // 1000
    return "%dd %02d:%02d:%02d" % (s // 86400, s // 3600 % 24, s // 60 % 60, s % 60)


def _describe(t, reading, actions, readback, before, after, cmds):
    shown = []
    for kind, payload in cmds:
        shown.append(payload if kind == CMD_WRITE else payload[0] if kind == CMD_EVENT else kind)
    return "%s %s %-9s -> %-9s ac=%r temp=%r hum=%r actions=%r cmds=%r" % (
        _clock(t),
        "rb  " if readback else "poll",
        before,
        after,
        reading.ac,
        reading.temp,
        reading.hum,
        [a.get("action") for a in actions],
        shown,
    )


class Scheduler:
    # Virtual time. t is ms since the start of the run (a plain int); the world clock moves with it.
    def __init__(self, clock):
        self.clock = clock
        self.t = 0
        self.entries = []  # (t, seq, fn), sorted
        self.seq = 0
        self.on_wake = None

    def at(self, t, fn):
        self.seq += 1
        e = (t, self.seq, fn)
        i = len(self.entries)
        while i > 0 and self.entries[i - 1][0] > t:
            i -= 1
        self.entries.insert(i, e)

    def _to(self, t):
        if t > self.t:
            self.clock.advance(t - self.t)
            self.t = t
        if self.on_wake is not None:
            self.on_wake(self.t)

    async def sleep(self, ms, event):
        # The board's sleep hook. Runs due entries in order; returns True as soon as `event` is set.
        if event is not None and event.is_set():
            return True
        end = self.t + max(0, ms)
        while self.entries and self.entries[0][0] <= end:
            due, _, fn = self.entries.pop(0)
            self._to(due)
            fn()
            await asyncio.sleep_ms(0)
            if event is not None and event.is_set():
                return True
        self._to(end)
        return event is not None and event.is_set()


class Monitor:
    # Invariants from observables: readings, actions, commands, controller state, device state, world time.
    def __init__(self, harness):
        self.h = harness
        s = config.default_settings()
        self.poll_ms = s["poll_s"] * 1000
        self.cap_ms = s["hard_cap_min"] * MIN_MS
        self.hold_ms = s["manual_hold_min"] * MIN_MS
        self.release_ms = s["manual_release_off_min"] * MIN_MS
        profiles = config.default_profiles()["profiles"]
        self.max_cooldown = max(r["cooldown_min"] for p in profiles.values() for r in p.get("rules", ()))
        self.counts = {k: 0 for k in COVERAGE}
        self.counts.update({"reboots": 0, "writes": 0, "ignored": 0, "lost_actions": 0})
        self.recent = []
        # World side: survives reboots, like the AC and state.json.
        self.open = False  # a cycle or takeover we own, until its hand-off
        self.start_ac = None  # what the AC read when we took it
        self.confirmed = None  # our last write a read showed applied
        self.pending = None  # our last write, not seen applied yet
        self.writes = 0
        self.pause_min = None
        self.pause_on = False
        self.pause_since = None
        self.boot(0)

    def boot(self, t):
        # A new App: what lived in RAM is gone.
        self.last_poll = t
        self.readback_due = False
        self.queued = []
        self.seen = None
        self.cyc_since = None
        self.stop_ok_since = None
        self.cool_since = None
        self.cool_ms = None
        self._manual_reset(t, None)

    def _manual_reset(self, t, ac):
        self.hold_from = t
        self.off_from = t
        self.off_ms = 0
        self.off_at = t if ac is not None and not ac.power else None
        self.hold_over = False
        self.exit_asked = False

    def fail(self, msg):
        raise Violation(msg)

    def wakeup(self, t):
        if t - self.last_poll > self.poll_ms + 1000:
            self.fail("no poll for %d s" % ((t - self.last_poll) // 1000))

    def step(self, ctl, reading, actions, readback, before, was_booting, cmds):
        t = self.h.sched.t
        dev = self.h.device
        after = ctl.state
        ac = reading.ac
        kinds = [p[0] for k, p in cmds if k == CMD_EVENT]
        writes = [p for k, p in cmds if k == CMD_WRITE]
        self.recent.append(_describe(t, reading, actions, readback, before, after, cmds))
        if len(self.recent) > RECENT:
            self.recent.pop(0)

        # The shell: reads what the device holds, sends exactly the writes the core asked for, reads each back.
        if ac is not None and ac != dev.ac():
            self.fail("read %r, the device holds %r" % (ac, dev.ac()))
        if dev.writes != self.writes:
            self.fail("%d writes sent, %d commanded" % (dev.writes, self.writes))
        if readback:
            if not self.readback_due:
                self.fail("read-back without a write")
            self.readback_due = False
        else:
            if self.readback_due:
                self.fail("write not read back")
            self.last_poll = t
        # 5: no write while unreachable, none in a read-back step, at most one per tick, only while we own the AC.
        if writes:
            if len(writes) > 1:
                self.fail("%d writes in one step" % len(writes))
            if readback:
                self.fail("write in a read-back step")
            if ac is None:
                self.fail("write while unreachable")
            if after not in OWNED:
                self.fail("write in %s" % after)
            self.readback_due = True
            self.writes += 1
            self.counts["writes"] += 1

        resolve = was_booting and not readback and ac is not None
        if not readback and was_booting and ac is None:
            self.queued.extend(actions)  # held by the controller until the boot is decided
            effective = []
        else:
            effective = self.queued + list(actions)
            self.queued = []
        names = [a.get("action") for a in effective]
        leave = any(a.get("cycle") == "leave" for a in effective if a.get("action") in (ACT_PAUSE, ACT_SET_PROFILE))
        self.counts["ignored"] += kinds.count("ignored")
        for k, name in ((EV_CYCLE_START, "cycles"), (EV_TAKEOVER, "takeovers"), (EV_WRITE_FAILED, "write_failed"),
                        (EV_HARD_CAP, "hard_caps"), (EV_UNREACHABLE, "outages"), (EV_STAGE, "stages")):
            self.counts[name] += kinds.count(k)

        # 7: never mistake our own write for a human, never write over a state we didn't set.
        pre_pending = self.pending
        if EV_MANUAL in kinds and _same(ac, pre_pending):
            self.fail("our own write %r taken for a human" % (pre_pending,))
        if _same(ac, self.pending):
            self.confirmed = self.pending
            self.pending = None
        if resolve:
            self.pending = None  # the controller judged it against this read
        if EV_CYCLE_START in kinds or EV_TAKEOVER in kinds:
            self.open = True
            self.start_ac = ac if ac is not None else self.seen
            self.confirmed = self.pending = pre_pending = None
        if writes:
            w = writes[0]
            if not any(_same(ac, ref) for ref in (self.start_ac, self.confirmed, pre_pending)):
                self.fail("wrote %r over %r, which we didn't set" % (w, ac))
            if pre_pending is not None and w == pre_pending and not w.power:
                self.counts["stop_retries"] += 1
            self.pending = w

        # 1: every owned cycle ends with off or a hand-off.
        if after in OWNED and not self.open:
            self.fail("%s without a cycle start" % after)
        if before in OWNED and after not in OWNED:
            if not leave and not any(k in kinds for k in HANDOFFS):
                self.fail("left %s without a hand-off" % before)
            self.open = False
            self.start_ac = self.confirmed = self.pending = None
        # 8: a cycle ends with the AC off.
        if EV_CYCLE_END in kinds and dev.ac().power:
            self.fail("cycle_end with the AC on: %r" % (dev.ac(),))

        booting = ctl.booting
        poll = not readback and ac is not None and not booting  # a reachable poll read
        # 1: hard cap, at the first reachable poll past it.
        if after in CYCLING and not booting:
            if before not in CYCLING or resolve or EV_CYCLE_START in kinds or EV_TAKEOVER in kinds:
                self.cyc_since = t
            if poll and t - self.cyc_since >= self.cap_ms:
                self.fail("%s for %d min, past the hard cap" % (after, (t - self.cyc_since) // MIN_MS))
        else:
            self.cyc_since = None

        # 2: STOPPING ends within 2 polls + settle once the device takes writes.
        healthy = dev.reachable() and not dev.reject and not dev.ignore
        if after == STOPPING and not booting and healthy:
            if self.stop_ok_since is None:
                self.stop_ok_since = t
            if t - self.stop_ok_since > 2 * self.poll_ms + SETTLE_MS:
                self.fail("STOPPING for %d s with the AC taking writes" % ((t - self.stop_ok_since) // 1000))
        else:
            self.stop_ok_since = None

        # 3, 6: MANUAL has a reason to end, and ends once it has one.
        if not readback:
            if ac is None:
                self.off_at = None
            elif before == MANUAL:
                if ac != self.seen:
                    self.hold_from = t  # a new change by the human restarts the hold
                    self.hold_over = False
                if ac.power:
                    self.off_ms = 0
                    self.off_at = None
                    self.off_from = t
                else:
                    if self.off_at is not None:
                        self.off_ms += t - self.off_at
                    self.off_at = t
            if ac is not None:
                self.seen = ac
        if before == MANUAL:
            if any(n in MANUAL_EXITS for n in names):
                self.exit_asked = True
            if ACT_STOP in names or ACT_CLEAR_MANUAL in names:
                self.hold_over = True
        if before == MANUAL and after != MANUAL:
            held = poll and t - self.hold_from >= self.hold_ms
            off = poll and not ac.power and t - self.off_from >= self.release_ms
            if not (held or off or self.exit_asked):
                self.fail("left MANUAL after %d min with no reason" % ((t - self.hold_from) // MIN_MS))
        if after == MANUAL:
            if writes:
                self.fail("write in MANUAL")
            if before != MANUAL:
                self.counts["holds"] += 1
                self._manual_reset(t, ac)
            elif poll:
                # A fan run by hand is left alone after the hold.
                if t - self.hold_from >= self.hold_ms and not (ac.power and ac.mode == MODE_FAN):
                    self.fail("MANUAL past the hold (%d min)" % ((t - self.hold_from) // MIN_MS))
                if self.hold_over:
                    self.fail("MANUAL after stop/clear_manual")
                if not ac.power and self.off_ms >= self.release_ms:
                    self.fail("MANUAL with the AC off for %d min" % (self.off_ms // MIN_MS))

        # 4: cooldown and a timed pause end on time; IDLE never sees the AC on.
        if after == COOLDOWN and not booting:
            if before != COOLDOWN or resolve:
                self.cool_since = t
                self.cool_ms = ctl.cycle["cooldown_min"] * MIN_MS
                if ctl.cycle["cooldown_min"] > self.max_cooldown:
                    self.fail("cooldown of %d min" % ctl.cycle["cooldown_min"])
            elif not readback and t - self.cool_since >= self.cool_ms:
                self.fail("COOLDOWN past %d min" % (self.cool_ms // MIN_MS))
        if resolve and self.pause_on:
            self.pause_since = t
        for a in effective:
            if a.get("action") == ACT_PAUSE:
                self.pause_on = True
                self.pause_min = a.get("minutes")
                self.pause_since = t
            elif a.get("action") == ACT_RESUME:
                self.pause_on = False
        if after == PAUSED:
            if not readback and not booting and self.pause_min is not None and self.pause_on:
                if t - self.pause_since >= self.pause_min * MIN_MS:
                    self.fail("PAUSED past %d min" % self.pause_min)
        elif before == PAUSED:
            self.pause_on = False
        if poll and after == IDLE and ac.power and ctl.active != OFF:
            self.fail("IDLE with the AC on")

    def reboot(self, ctl, mid_write):
        where = ", after a write" if mid_write else ""
        self.recent.append("%s power cut in %s%s" % (_clock(self.h.sched.t), ctl.state, where))
        self.counts["reboots"] += 1
        if ctl.state in OWNED:
            self.counts["reboots_mid_cycle"] += 1
        if mid_write:
            self.counts["reboots_mid_write"] += 1


class _Client(DaikinClient):
    # Cuts the power right after a write reached the AC, when the timeline asks for it.
    def __init__(self, harness, client):
        super().__init__(client.ip, client.port, client.timeout_ms, client.limits)
        self.harness = harness

    async def write(self, state):
        err = await super().write(state)
        h = self.harness
        if h.cut is not None:
            arg, h.cut = h.cut, None
            raise Reboot(arg, True)
        return err


class _App(App):
    def __init__(self, harness, board):
        super().__init__(board, harness.store, "127.0.0.1", harness.port)
        self.harness = harness
        self.client = _Client(harness, self.client)

    def _step(self, now, reading, actions, readback=False):
        ctl = self.controller
        before = ctl.state
        was_booting = ctl.booting
        cmds = super()._step(now, reading, actions, readback)
        self.harness.monitor.step(ctl, reading, actions, readback, before, was_booting, cmds)
        return cmds


class Harness:
    def __init__(self, seed=1, days=30, port=DEFAULT_PORT, quiet=True):
        self.seed = seed
        self.days = days
        self.port = port
        self.quiet = quiet
        self.scenario = scenario.generate(seed, days)
        self.clock = FakeClock(time.ticks_add(0, -DAY_MS))  # the world crosses the wrap on day 1 too
        self.sched = Scheduler(self.clock)
        self.sched.on_wake = self._wake
        # The AC's clock: the run starts at 00:00, so the Day and Night schedules go through their stages.
        wall = wallclock.days(2026, 1, 1) * wallclock.DAY_S
        self.device = FakeDaikin(self.clock, script=self.scenario.script, wall=wall)
        self.store = MemoryStore()
        self.monitor = Monitor(self)
        self.app = None
        self.boards = []
        self.cut = None  # reboot arg, armed for right after the next write
        self.in_settle = False
        self.day = 0

    def _wake(self, t):
        if self.app is not None:
            self.monitor.wakeup(t)
        if not self.quiet and t // DAY_MS > self.day:
            self.day = t // DAY_MS
            print("day %d: %s, %r" % (self.day, self.app.controller.state if self.app else "down", self.monitor.counts))

    async def _sleep(self, ms, event):
        self.in_settle = event is None  # only the read-back settle sleeps without an event
        try:
            return await self.sched.sleep(ms, event)
        finally:
            self.in_settle = False

    def _entry(self, kind, arg):
        def fire():
            if self.app is None:
                if kind == "action":
                    self.monitor.counts["lost_actions"] += 1  # the Pico is down, the page gets no answer
                return
            if kind == "action":
                if not self.app.action(dict(arg)):
                    self.monitor.counts["lost_actions"] += 1
            elif kind == "reboot":
                if arg["after_write"]:
                    self.cut = arg
                else:
                    raise Reboot(arg, self.in_settle)

        return fire

    def _end(self):
        raise Finished()

    async def _down(self, ms):
        self.app = None
        try:
            await self.sched.sleep(ms, None)
        except Finished:
            return False
        return True

    async def run(self):
        # -> the counts. Raises Violation with a report on the first broken invariant.
        server = await serve(self.device, port=self.port)
        try:
            for s, kind, arg in self.scenario.timeline:
                self.sched.at(s * 1000, self._entry(kind, arg))
            self.sched.at(self.scenario.end_min * MIN_MS, self._end)
            offset = 0
            while True:
                board = HostBoard(clock=self.clock, sleep=self._sleep, ticks_offset=offset)
                self.boards.append(board)
                self.monitor.boot(self.sched.t)
                self.app = _App(self, board)
                try:
                    await self.app.control()
                except Reboot as e:
                    arg, mid_write = e.args
                    self.monitor.reboot(self.app.controller, mid_write)
                    if not await self._down(arg["down_s"] * 1000):
                        break
                    before_wrap = arg["before_wrap_min"] * MIN_MS
                    offset = time.ticks_diff(time.ticks_add(0, -before_wrap), self.clock())
                except Finished:
                    break
            self._final()
        except Violation as e:
            raise Violation(self._report(str(e)))
        finally:
            server.close()
            await server.wait_closed()
        return self.monitor.counts

    def _final(self):
        m = self.monitor
        m.counts["wraps"] = sum(b.wraps for b in self.boards)
        ctl = self.app.controller if self.app is not None else None
        if m.counts["wraps"] < 1:
            m.fail("the ticks never wrapped")
        if ctl is None:
            m.fail("the run ended with the Pico down")
        if ctl.booting or ctl.state != IDLE or ctl.active != OFF:
            m.fail("did not settle: %s, profile %s" % (ctl.state, ctl.active))
        if m.open or self.device.ac().power:
            m.fail("did not settle: AC %r" % (self.device.ac(),))

    def _report(self, msg):
        d = self.device
        lines = [
            "VIOLATION: %s" % msg,
            "seed %d, %d days, world %s (minute %d)" % (self.seed, self.days, _clock(self.sched.t),
                                                        self.sched.t // MIN_MS),
            "device: %r, reachable %r, reject %d, ignore %d, room %.1f C %.0f %%"
            % (d.ac(), d.reachable(), d.reject, d.ignore, d.room.temp, d.room.hum),
            "recent steps:",
        ]
        lines += ["  " + s for s in self.monitor.recent]
        return "\n".join(lines)


def run(seed=1, days=30, port=DEFAULT_PORT, quiet=True):
    return asyncio.run(Harness(seed, days, port, quiet).run())


def main(argv):
    days, seed, port, quiet, show = 30, 1, DEFAULT_PORT, False, False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--days":
            i += 1
            days = int(argv[i])
        elif a == "--seed":
            i += 1
            seed = int(argv[i])
        elif a == "--port":
            i += 1
            port = int(argv[i])
        elif a == "--quiet":
            quiet = True
        elif a == "--show":
            show = True
        else:
            print("usage: micropython sim/harness.py [--days N] [--seed N] [--port N] [--quiet] [--show]")
            return 2
        i += 1
    if show:
        for line in scenario.generate(seed, days).lines():
            print(line)
        return 0
    start = time.ticks_ms()
    try:
        counts = run(seed, days, port, quiet)
    except Violation as e:
        print(e)
        return 1
    secs = time.ticks_diff(time.ticks_ms(), start) / 1000
    summary = " ".join("%s=%d" % kv for kv in sorted(counts.items()))
    print("seed %d, %d days in %.1f s: %s" % (seed, days, secs, summary))
    if days >= 30:
        missing = [k for k in COVERAGE if counts[k] < 1]
        if missing:
            print("NOT COVERED: %s" % ", ".join(missing))
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
