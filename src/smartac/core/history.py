# SPDX-License-Identifier: GPL-3.0-only
# 24 h of one-minute samples in RAM (idea.md, "History storage"): preallocated arrays, about 10 KB, lost on reboot.
# A sample's slot is uptime_ms // step_ms. Uptime is summed by the app tick to tick, so it never wraps
# (ADR 0004). Slots skipped between two samples, and unreachable readings, are gaps.
# chunks() streams the /api/history JSON in small strings, so the whole response is never built in RAM.
from array import array

from smartac.core.types import MODE_UNKNOWN, MODES

GAP = -32768  # in the 'h' arrays (temps in tenths)
GAP_B = -1  # in the 'b' arrays (hum, mode, power)
CODES = MODES + (MODE_UNKNOWN,)  # a mode's code is its index here
BATCH = 60  # values per yielded string
SERIES = ("temp", "hum", "out", "mode", "power")


def _tenths(v):
    # Tenths -> "28.5", without floats.
    sign = "-" if v < 0 else ""
    v = abs(v)
    return "%s%d.%d" % (sign, v // 10, v % 10)


class History:
    def __init__(self, slots=1440, step_ms=60000):
        self.slots = slots
        self.step_ms = step_ms
        self.temp = array("h", [GAP] * slots)
        self.out = array("h", [GAP] * slots)
        self.hum = array("b", [GAP_B] * slots)
        self.mode = array("b", [GAP_B] * slots)
        self.power = array("b", [GAP_B] * slots)
        self.last = None  # the newest slot written
        self.last_ms = None  # uptime of the newest sample

    def add(self, uptime_ms, reading):
        s = uptime_ms // self.step_ms
        if self.last is not None:
            if s < self.last:
                return  # uptime only moves forward; ignore anything else
            for k in range(max(self.last + 1, s - self.slots + 1), s):
                self._put(k % self.slots, None)
        self._put(s % self.slots, reading)
        self.last = s
        self.last_ms = uptime_ms

    def _put(self, i, r):
        if r is None or r.ac is None:
            self.temp[i] = self.out[i] = GAP
            self.hum[i] = self.mode[i] = self.power[i] = GAP_B
            return
        self.temp[i] = GAP if r.temp is None else r.temp
        self.out[i] = GAP if r.outdoor_temp is None else r.outdoor_temp
        self.hum[i] = GAP_B if r.hum is None else r.hum
        self.mode[i] = CODES.index(r.ac.mode) if r.ac.mode in CODES else CODES.index(MODE_UNKNOWN)
        self.power[i] = 1 if r.ac.power else 0

    def last_age_s(self, uptime_ms):
        if self.last_ms is None:
            return None
        return (uptime_ms - self.last_ms) // 1000

    def clamp(self, minutes):
        # Any query value -> 1..slots; None or garbage is the whole day.
        try:
            minutes = int(minutes)
        except (TypeError, ValueError):
            return self.slots
        return min(max(minutes, 1), self.slots)

    def chunks(self, uptime_ms, minutes=None):
        # The last `minutes` slots up to the newest sample, oldest first. Slots never written are gaps.
        n = self.clamp(minutes)
        age = self.last_age_s(uptime_ms)
        yield '{"step_s": %d, "last_age_s": %s' % (self.step_ms // 1000, "null" if age is None else age)
        end = -1 if self.last is None else self.last
        first = end - n + 1
        for name in SERIES:
            yield ', "%s": [' % name
            for start in range(first, end + 1, BATCH):
                parts = []
                for k in range(start, min(start + BATCH, end + 1)):
                    parts.append(self._value(name, k % self.slots) if k >= 0 else "null")
                yield ("" if start == first else ", ") + ", ".join(parts)
            yield "]"
        yield "}"

    def _value(self, name, i):
        if name == "temp" or name == "out":
            v = (self.temp if name == "temp" else self.out)[i]
            return "null" if v == GAP else _tenths(v)
        if name == "hum":
            v = self.hum[i]
            return "null" if v == GAP_B else str(v)
        if name == "mode":
            v = self.mode[i]
            return "null" if v == GAP_B else '"%s"' % CODES[v]
        v = self.power[i]
        return "null" if v == GAP_B else ("true" if v else "false")
