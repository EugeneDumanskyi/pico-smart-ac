# SPDX-License-Identifier: GPL-3.0-only
# HostBoard: ticks from any clock with an offset, the wrap counter, wait() in real time and through a sleep hook.
import asyncio
import time
import unittest

from sim.clock import FakeClock
from smartac.board.host import HostBoard


class TestTicks(unittest.TestCase):
    def test_offset_is_added_to_the_clock(self):
        clock = FakeClock(1000)
        board = HostBoard(clock=clock, ticks_offset=500)
        self.assertEqual(board.ticks_ms(), 1500)
        clock.advance(250)
        self.assertEqual(board.ticks_ms(), 1750)

    def test_counts_wraps(self):
        clock = FakeClock()
        board = HostBoard(clock=clock, ticks_offset=-1000)  # a signed delta, as ticks_add takes it
        board.ticks_ms()
        clock.advance(999)
        board.ticks_ms()
        self.assertEqual(board.wraps, 0)
        clock.advance(2)
        self.assertEqual(time.ticks_diff(board.ticks_ms(), time.ticks_add(0, -1000)), 1001)
        self.assertEqual(board.wraps, 1)


class TestWait(unittest.TestCase):
    def test_event_set_first(self):
        board = HostBoard()
        event = asyncio.Event()

        async def main():
            async def setter():
                await asyncio.sleep_ms(5)
                event.set()

            asyncio.create_task(setter())
            start = time.ticks_ms()
            got = await board.wait(event, 2000)
            return got, time.ticks_diff(time.ticks_ms(), start)

        got, took = asyncio.run(main())
        self.assertTrue(got)
        self.assertTrue(took < 1000, took)

    def test_timeout(self):
        board = HostBoard()
        self.assertFalse(asyncio.run(board.wait(asyncio.Event(), 10)))
        self.assertFalse(asyncio.run(board.wait(None, 10)))

    def test_event_already_set(self):
        event = asyncio.Event()
        event.set()
        self.assertTrue(asyncio.run(HostBoard().wait(event, 10000)))

    def test_speed_scales_the_sleep(self):
        board = HostBoard(speed=1000)
        start = time.ticks_ms()
        asyncio.run(board.sleep_ms(5000))
        self.assertTrue(time.ticks_diff(time.ticks_ms(), start) < 1000)

    def test_sleep_hook_replaces_waiting(self):
        calls = []

        async def hook(ms, event):
            calls.append((ms, event))
            return True

        event = asyncio.Event()
        board = HostBoard(sleep=hook)
        self.assertTrue(asyncio.run(board.wait(event, 60000)))
        asyncio.run(board.sleep_ms(5000))
        self.assertEqual(calls, [(60000, event), (5000, None)])

    def test_no_op_hardware(self):
        board = HostBoard()
        board.wdt_start(8000)
        board.wdt_feed()
        board.led("error")
        board.reset()
        self.assertEqual((board.wdt_ms, board.wdt_feeds, board.led_pattern, board.resets), (8000, 1, "error", 1))
        self.assertEqual(board.reset_cause(), "power_on")
        self.assertTrue(board.link_up())
