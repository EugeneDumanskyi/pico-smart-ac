# SPDX-License-Identifier: GPL-3.0-only
# Board for the Unix port: time from any clock callable (sim/clock.py), no hardware.
# Real time by default; `speed` scales the sleeps for a ScaledClock of the same speed; a `sleep` hook
# (async (ms, event) -> bool) replaces the sleeps entirely, for virtual time (sim/harness.py).
# Watchdog, LED, reset and Wi-Fi only count calls; `link` is the fake Wi-Fi state, and `on_connect` (if set)
# is called by wifi_connect(), so a test can bring the link back.
import gc
import time

from smartac.board.base import POWER_ON, Board, wait_event


class HostBoard(Board):
    def __init__(self, clock=time.ticks_ms, speed=1, sleep=None, ticks_offset=0, cause=POWER_ON):
        # ticks_offset is added to the clock, so each simulated boot can start its ticks anywhere.
        self.clock = clock
        self.speed = speed
        self.sleep = sleep
        self.ticks_offset = ticks_offset
        self.cause = cause
        self.wraps = 0  # times ticks_ms() was seen to wrap
        self._last = None
        self.wdt_ms = None
        self.wdt_feeds = 0
        self.led_pattern = None
        self.resets = 0
        self.reset_reasons = []
        self.link = True
        self.connects = 0
        self.disconnects = 0
        self.on_connect = None

    def ticks_ms(self):
        now = time.ticks_add(self.clock(), self.ticks_offset)
        if self._last is not None and now < self._last:
            self.wraps += 1  # the clock only moves forward, so a smaller raw value is a wrap
        self._last = now
        return now

    async def wait(self, event, ms):
        if self.sleep is not None:
            return await self.sleep(ms, event)
        return await wait_event(event, max(ms, 0) / self.speed)

    def wdt_start(self, ms):
        self.wdt_ms = ms

    def wdt_feed(self):
        self.wdt_feeds += 1

    def led(self, pattern):
        self.led_pattern = pattern

    def reset(self, reason):
        self.resets += 1
        self.reset_reasons.append(reason)

    def reset_cause(self):
        return self.cause

    def link_up(self):
        return self.link

    def wifi_connect(self):
        self.connects += 1
        if self.on_connect is not None:
            self.on_connect()

    def wifi_disconnect(self):
        self.disconnects += 1
        self.link = False

    def rssi(self):
        return -50 if self.link else None

    def heap(self):
        return gc.mem_free(), None

    def device_info(self):
        return {"fw": "host", "ip": "127.0.0.1", "rssi": self.rssi()}
