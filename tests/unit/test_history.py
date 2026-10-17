# SPDX-License-Identifier: GPL-3.0-only
import json
import unittest

from smartac.core import types as t
from smartac.core.history import History

MIN = 60 * 1000
DAY_MIN = 24 * 60


def reading(temp=285, hum=81, out=315, mode=t.MODE_DRY, power=True):
    return t.Reading(t.ACState(power, mode, None, t.FAN_AUTO), temp=temp, hum=hum, outdoor_temp=out)


def export(h, uptime_ms, minutes=None):
    return json.loads("".join(h.chunks(uptime_ms, minutes)))


class TestSlots(unittest.TestCase):
    def test_one_sample(self):
        h = History(slots=5)
        h.add(2 * MIN + 12000, reading())
        d = export(h, 2 * MIN + 20000)
        self.assertEqual((d["step_s"], d["last_age_s"]), (60, 8))
        self.assertEqual(d["temp"], [None, None, None, None, 28.5])
        self.assertEqual(d["hum"][-1], 81)
        self.assertEqual(d["out"][-1], 31.5)
        self.assertEqual(d["mode"][-1], "dry")
        self.assertEqual(d["power"][-1], True)

    def test_skipped_slots_and_unreachable_are_gaps(self):
        h = History(slots=6)
        h.add(0, reading(temp=280))
        h.add(1 * MIN, t.Reading.unreachable("timeout"))
        h.add(4 * MIN, reading(temp=290, power=False, mode=t.MODE_COOL))
        d = export(h, 4 * MIN)
        self.assertEqual(d["temp"], [None, 28.0, None, None, None, 29.0])
        self.assertEqual(d["power"], [None, True, None, None, None, False])
        self.assertEqual(d["mode"], [None, "dry", None, None, None, "cool"])

    def test_missing_sensor_is_a_gap_in_that_series_only(self):
        h = History(slots=2)
        h.add(0, reading(temp=None, out=None))
        d = export(h, 0)
        self.assertEqual((d["temp"][-1], d["out"][-1], d["hum"][-1], d["power"][-1]), (None, None, 81, True))

    def test_same_slot_keeps_the_newest(self):
        h = History(slots=3)
        h.add(10000, reading(temp=280))
        h.add(50000, reading(temp=275))
        self.assertEqual(export(h, 50000)["temp"], [None, None, 27.5])

    def test_ring_overwrites_the_oldest(self):
        h = History(slots=4)
        for m in range(10):
            h.add(m * MIN, reading(temp=200 + m))
        self.assertEqual(export(h, 9 * MIN)["temp"], [20.6, 20.7, 20.8, 20.9])

    def test_long_gap_clears_the_ring(self):
        h = History(slots=4)
        for m in range(4):
            h.add(m * MIN, reading())
        h.add(100 * MIN, reading(temp=300))
        self.assertEqual(export(h, 100 * MIN)["temp"], [None, None, None, 30.0])

    def test_uptime_past_the_ticks_wrap(self):
        # Uptime is a sum, so it keeps growing where the Pico's ticks wrap (2**30 ms, about 12.4 days).
        h = History()
        base = 40 * DAY_MIN * MIN
        h.add(base - MIN, reading(temp=280))
        h.add(base, reading(temp=281))
        d = export(h, base + 3000)
        self.assertEqual((d["temp"][-2:], d["last_age_s"]), ([28.0, 28.1], 3))

    def test_older_sample_is_ignored(self):
        h = History(slots=3)
        h.add(2 * MIN, reading(temp=280))
        h.add(1 * MIN, reading(temp=300))
        self.assertEqual(export(h, 2 * MIN)["temp"], [None, None, 28.0])

    def test_negative_temp(self):
        h = History(slots=1)
        h.add(0, reading(out=-5))
        self.assertEqual(export(h, 0)["out"], [-0.5])


class TestExport(unittest.TestCase):
    def test_empty_is_all_gaps(self):
        d = export(History(), 0)
        self.assertIsNone(d["last_age_s"])
        for name in ("temp", "hum", "out", "mode", "power"):
            self.assertEqual(d[name], [None] * DAY_MIN)

    def test_full_day_parses(self):
        h = History()
        for m in range(DAY_MIN + 30):
            h.add(m * MIN, reading(temp=250 + m % 50, hum=m % 100))
        d = export(h, (DAY_MIN + 29) * MIN)
        self.assertEqual(len(d["temp"]), DAY_MIN)
        self.assertEqual(d["hum"][-1], (DAY_MIN + 29) % 100)

    def test_chunks_stay_small(self):
        h = History()
        for m in range(DAY_MIN):
            h.add(m * MIN, reading())
        self.assertTrue(max(len(c) for c in h.chunks(DAY_MIN * MIN)) < 1024)

    def test_minutes_are_clamped(self):
        h = History(slots=10)
        h.add(0, reading())
        self.assertEqual(len(export(h, 0, 3)["temp"]), 3)
        self.assertEqual(len(export(h, 0, 0)["temp"]), 1)
        self.assertEqual(len(export(h, 0, -7)["temp"]), 1)
        self.assertEqual(len(export(h, 0, 99)["temp"]), 10)
        self.assertEqual(len(export(h, 0, "abc")["temp"]), 10)
        self.assertEqual(len(export(h, 0, "4")["temp"]), 4)
        self.assertEqual(export(h, 0, 1)["temp"], [28.5])
