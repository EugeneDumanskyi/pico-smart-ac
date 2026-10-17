# SPDX-License-Identifier: GPL-3.0-only
# NetSupervisor (shell/net.py) in virtual time: HostBoard's fake link, the harness Scheduler as the sleep hook.
import asyncio
import unittest

from sim.clock import FakeClock
from sim.harness import Scheduler
from smartac.board.host import HostBoard
from smartac.shell import net

S = 1000
MIN = 60 * S


class Stop(Exception):
    pass


class FakeApp:
    # What the supervisor reads from the app: seen_ac, unreachable_ms() and the stats it writes.
    def __init__(self, clock):
        self.clock = clock
        self.seen_ac = True
        self.since = None  # clock ms the AC went silent, None while it answers
        self.stats = {}

    def unreachable_ms(self):
        return None if self.since is None else self.clock() - self.since


class Rig:
    def __init__(self, link=True):
        self.clock = FakeClock()
        self.sched = Scheduler(self.clock)
        self.board = HostBoard(clock=self.clock, sleep=self.sched.sleep)
        self.board.link = link
        self.app = FakeApp(self.clock)
        self.tries = []  # virtual times of wifi_connect()
        self.resets = []  # (time, reason)
        self.reconnect_after = 0  # ms until a connect brings the link up; None: never
        self.board.on_connect = self._connect
        reset = self.board.reset

        def record(reason):
            self.resets.append((self.sched.t, reason))
            reset(reason)

        self.board.reset = record
        self.net = net.NetSupervisor(self.board, self.app)

    def _connect(self):
        self.tries.append(self.sched.t)
        if self.reconnect_after == 0:
            self.board.link = True
        elif self.reconnect_after is not None:
            self.at(self.sched.t + self.reconnect_after, lambda: self.set_link(True))

    def set_link(self, up):
        self.board.link = up

    def at(self, t, fn):
        self.sched.at(t, fn)

    def run(self, until):
        def stop():
            raise Stop()

        async def main():
            try:
                self.sched.at(until, stop)
                await self.net.run()
            except Stop:
                pass

        asyncio.run(main())
        return self.app.stats


class TestLink(unittest.TestCase):
    def test_first_connect_is_not_a_reconnect(self):
        rig = Rig(link=False)
        stats = rig.run(1 * MIN)
        self.assertEqual(rig.tries, [0])
        self.assertEqual(stats, {"wifi_drops": 0, "wifi_reconnects": 0, "ac_reconnects": 0})
        self.assertEqual(rig.board.led_pattern, "off")

    def test_drop_backs_off_then_recovers(self):
        rig = Rig()
        rig.reconnect_after = None
        rig.at(17 * S, lambda: rig.set_link(False))
        rig.at(120 * S, lambda: rig.set_link(True))
        stats = rig.run(5 * MIN)
        self.assertEqual(rig.tries, [20 * S, 25 * S, 35 * S, 55 * S, 95 * S])
        self.assertEqual((stats["wifi_drops"], stats["wifi_reconnects"]), (1, 1))
        self.assertEqual(rig.resets, [])

    def test_backoff_caps_at_a_minute_and_restarts_after_recovery(self):
        rig = Rig()
        rig.reconnect_after = None
        rig.at(1 * S, lambda: rig.set_link(False))
        rig.at(5 * MIN + 1 * S, lambda: rig.set_link(True))
        rig.at(6 * MIN + 1 * S, lambda: rig.set_link(False))
        rig.run(7 * MIN)
        gaps = [b - a for a, b in zip(rig.tries, rig.tries[1:])]
        self.assertEqual(gaps[:6], [5 * S, 10 * S, 20 * S, 40 * S, 60 * S, 60 * S])
        self.assertEqual([t for t in rig.tries if t > 6 * MIN], [6 * MIN + 5 * S, 6 * MIN + 10 * S, 6 * MIN + 20 * S,
                                                                 6 * MIN + 40 * S])

    def test_reset_after_15_min_down(self):
        rig = Rig()
        rig.reconnect_after = None
        rig.at(17 * S, lambda: rig.set_link(False))
        rig.run(20 * MIN)
        self.assertEqual(rig.resets[0], (20 * S + 15 * MIN, "wifi_down"))

    def test_no_reset_when_back_within_15_min(self):
        rig = Rig()
        rig.reconnect_after = None
        rig.at(17 * S, lambda: rig.set_link(False))
        rig.at(14 * MIN, lambda: rig.set_link(True))
        stats = rig.run(30 * MIN)
        self.assertEqual(rig.resets, [])
        self.assertEqual(stats["wifi_reconnects"], 1)

    def test_heartbeat_and_led(self):
        rig = Rig()
        rig.reconnect_after = None
        rig.at(1 * S, lambda: rig.set_link(False))
        rig.run(12 * S)
        self.assertEqual((rig.net.beat, rig.board.led_pattern), (10 * S, "error"))


class TestSilentAC(unittest.TestCase):
    def test_reconnect_at_10_min_reset_at_30_after_seen(self):
        rig = Rig()
        rig.app.since = 0
        stats = rig.run(40 * MIN)
        self.assertEqual(rig.tries, [10 * MIN])
        self.assertEqual((rig.board.disconnects, stats["ac_reconnects"]), (1, 1))
        self.assertEqual((stats["wifi_drops"], stats["wifi_reconnects"]), (0, 0))
        self.assertEqual(rig.resets[0], (30 * MIN, "ac_silent"))
        self.assertEqual(rig.board.led_pattern, "on")

    def test_never_seen_reconnects_once_and_stays_up(self):
        rig = Rig()
        rig.app.seen_ac = False
        rig.app.since = 0
        stats = rig.run(3 * 60 * MIN)
        self.assertEqual((rig.tries, stats["ac_reconnects"], rig.resets), ([10 * MIN], 1, []))

    def test_slow_forced_reconnect_is_not_a_drop(self):
        rig = Rig()
        rig.app.since = 0
        rig.reconnect_after = 8 * S
        stats = rig.run(31 * MIN)
        self.assertEqual((stats["wifi_drops"], stats["wifi_reconnects"], stats["ac_reconnects"]), (0, 0, 1))
        self.assertEqual(rig.resets[0], (30 * MIN, "ac_silent"))  # the forced reconnect didn't restart the clock

    def test_quick_forced_reconnect_doesnt_hide_the_next_drop(self):
        rig = Rig()
        rig.app.since = 0
        rig.at(11 * MIN, lambda: setattr(rig.app, "since", None))
        rig.at(12 * MIN + 1 * S, lambda: rig.set_link(False))
        stats = rig.run(13 * MIN)
        self.assertEqual((stats["ac_reconnects"], stats["wifi_drops"]), (1, 1))

    def test_answering_again_rearms_the_reconnect(self):
        rig = Rig()
        rig.app.since = 0
        rig.at(11 * MIN, lambda: setattr(rig.app, "since", None))
        rig.at(12 * MIN, lambda: setattr(rig.app, "since", 12 * MIN))
        stats = rig.run(25 * MIN)
        self.assertEqual((rig.tries, stats["ac_reconnects"]), ([10 * MIN, 22 * MIN], 2))

    def test_silence_counts_from_link_up(self):
        # Wi-Fi down for 14 min: the AC was unreachable all along, but only the time since the link came back counts.
        rig = Rig(link=False)
        rig.reconnect_after = None
        rig.app.since = 0

        def back():
            rig.set_link(True)
            rig.reconnect_after = 0

        rig.at(14 * MIN, back)
        rig.run(45 * MIN)
        self.assertEqual(rig.tries[-1], 24 * MIN)
        self.assertEqual(rig.resets[0], (44 * MIN, "ac_silent"))
