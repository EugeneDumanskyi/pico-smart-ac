# SPDX-License-Identifier: GPL-3.0-only
# Clocks for the simulator and the host app: anything callable that returns ticks in ms works (ADR 0004).
import time

MIN_MS = 60 * 1000


class FakeClock:
    # Time moves only when told to. Start near the wrap (e.g. time.ticks_add(0, -MIN_MS)) to test it.
    def __init__(self, start=0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, ms):
        self.now = time.ticks_add(self.now, int(ms))

    def advance_min(self, n):
        self.advance(n * MIN_MS)


class ScaledClock:
    # Real time times `speed`. Accumulates per call, so long runs wrap the ticks like a real long uptime.
    def __init__(self, speed=1, start=0):
        self.speed = speed
        self.now = start
        self._real = time.ticks_ms()
        self._carry = 0.0

    def __call__(self):
        real = time.ticks_ms()
        scaled = time.ticks_diff(real, self._real) * self.speed + self._carry
        self._real = real
        step = int(scaled)
        self._carry = scaled - step
        self.now = time.ticks_add(self.now, step)
        return self.now
