# SPDX-License-Identifier: GPL-3.0-only
# Board for the Unix port: time from any clock callable (sim/clock.py), no hardware.
# Real time by default; `speed` scales the sleeps for a ScaledClock of the same speed; a `sleep` hook
# (async (ms, event) -> bool) replaces the sleeps entirely, for virtual time (sim/harness.py).
# Watchdog, LED and reset only count calls.
import asyncio
import time

from smartac.board.base import Board


class HostBoard(Board):
    def __init__(self, clock=time.ticks_ms, speed=1, sleep=None, ticks_offset=0):
        # ticks_offset is added to the clock, so each simulated boot can start its ticks anywhere.
        self.clock = clock
        self.speed = speed
        self.sleep = sleep
        self.ticks_offset = ticks_offset
        self.wraps = 0  # times ticks_ms() was seen to wrap
        self._last = None
        self.wdt_ms = None
        self.wdt_feeds = 0
        self.led_pattern = None
        self.resets = 0

    def ticks_ms(self):
        now = time.ticks_add(self.clock(), self.ticks_offset)
        if self._last is not None and now < self._last:
            self.wraps += 1  # the clock only moves forward, so a smaller raw value is a wrap
        self._last = now
        return now

    async def wait(self, event, ms):
        if self.sleep is not None:
            return await self.sleep(ms, event)
        if event is not None and event.is_set():
            return True
        ms = int(max(ms, 0) / self.speed)
        if event is None:
            await asyncio.sleep_ms(ms)
            return False
        try:
            await asyncio.wait_for_ms(event.wait(), ms)
        except asyncio.TimeoutError:
            return False
        return True

    def wdt_start(self, ms):
        self.wdt_ms = ms

    def wdt_feed(self):
        self.wdt_feeds += 1

    def led(self, pattern):
        self.led_pattern = pattern

    def reset(self):
        self.resets += 1

    def reset_cause(self):
        return "power_on"

    def link_up(self):
        return True
