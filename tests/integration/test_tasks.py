# SPDX-License-Identifier: GPL-3.0-only
# shell/tasks.py: the watchdog feeds only on fresh heartbeats, housekeeping's heap stats, crashes reach the store.
import asyncio
import json
import unittest

from sim.clock import FakeClock
from sim.harness import Scheduler
from sim.server import DEFAULT_PORT
from smartac.board.host import HostBoard
from smartac.core import types as t
from smartac.shell import tasks
from smartac.shell.app import App
from smartac.shell.store import MemoryStore

S = 1000
PORT = DEFAULT_PORT + 2


class Stop(Exception):
    pass


class Beats:
    # The heartbeat fields the watchdog reads from the app and the net supervisor.
    def __init__(self):
        self.beat = 0

    def poll_ms(self):
        return 60 * S


def virtual(coro_fn, until, sched):
    def stop():
        raise Stop()

    async def main():
        try:
            sched.at(until, stop)
            await coro_fn()
        except Stop:
            pass

    asyncio.run(main())


class TestWatchdog(unittest.TestCase):
    def test_feeds_only_while_both_beat(self):
        clock = FakeClock()
        sched = Scheduler(clock)
        board = HostBoard(clock=clock, sleep=sched.sleep)
        app, net = Beats(), Beats()
        fed = []
        board.wdt_feed = lambda: fed.append(sched.t)
        sched.at(25 * S, lambda: setattr(net, "beat", 25 * S))
        virtual(lambda: tasks.watchdog(board, app, net), 3 * 60 * S, sched)
        self.assertEqual(board.wdt_ms, tasks.WDT_MS)
        # net beat at 0 and 25 s: fresh until 55 s. control beat at 0: fresh for 2 x poll = 120 s.
        self.assertEqual((fed[0], fed[-1], len(fed)), (0, 54 * S, 55))


class TestHousekeeping(unittest.TestCase):
    def test_stats_and_one_event_per_low_spell(self):
        clock = FakeClock()
        sched = Scheduler(clock)
        board = HostBoard(clock=clock, sleep=sched.sleep)
        store = MemoryStore()
        app = App(board, store, observe=True)
        heaps = [(200000, 90000), (50000, 90000), (40000, 10000), (200000, 90000), (200000, 20000)]
        board.heap = lambda: heaps.pop(0) if len(heaps) > 1 else heaps[0]
        virtual(lambda: tasks.housekeeping(board, app), 4 * 60 * S + 1, sched)
        st = app.stats
        self.assertEqual((st["heap_free"], st["heap_max_block"]), (200000, 20000))
        self.assertEqual((st["heap_min_free"], st["heap_min_block"], st["low_memory"]), (40000, 10000, 2))
        kinds = [r["kind"] for r in app.events.export(board.ticks_ms())]
        self.assertEqual(kinds.count(t.EV_LOW_MEMORY), 2)


class TestCrash(unittest.TestCase):
    def setUp(self):
        tasks.print = lambda *args: None  # run() prints the traceback for the serial log

    def tearDown(self):
        del tasks.print
        asyncio.new_event_loop()  # drop the tasks a crashed run left in the queue

    def test_crash_at_boot(self):
        board, store = HostBoard(), MemoryStore()

        def make():
            raise ValueError("bad settings")

        tasks.run(board, store, make, port=PORT)
        self.assertEqual(board.reset_reasons, ["crash"])
        self.assertTrue(store.load_crash().strip().endswith("ValueError: bad settings"))

    def test_crash_in_a_task(self):
        board, store = HostBoard(), MemoryStore()

        class Crashing(App):
            async def control(self):
                await asyncio.sleep_ms(20)
                raise RuntimeError("stuck")

        tasks.run(board, store, lambda: Crashing(board, store, observe=True), port=PORT)
        self.assertEqual(board.reset_reasons, ["crash"])
        text = store.load_crash()
        self.assertTrue("Traceback" in text and text.strip().endswith("RuntimeError: stuck"), text)
        self.assertEqual(board.wdt_ms, tasks.WDT_MS)
        # The next boot shows it.
        app = App(HostBoard(cause="crash"), store, observe=True)
        d = app.status()["device"]
        self.assertEqual((d["last_crash"], d["reset_cause"], d["resets"]), ("RuntimeError: stuck", "crash",
                                                                            {"power_on": 1, "crash": 1}))
        json.dumps(app.status())

    def test_port_is_free_after_a_crash(self):
        self.test_crash_in_a_task()
        self.test_crash_in_a_task()
