# SPDX-License-Identifier: GPL-3.0-only
# Room model: direction and rough size of each effect, calibrated against the 2026-10-08 run.
import unittest

from sim.room import ROOM_DEFAULTS, Room
from smartac.core import types as t

COOL = t.ACState(True, t.MODE_COOL, 260, t.FAN_AUTO)
DRY = t.ACState(True, t.MODE_DRY, None, t.FAN_AUTO)
FAN = t.ACState(True, t.MODE_FAN, None, "3")
OFF = t.ACState(False, t.MODE_FAN, None, "3")


def run(room, ac, minutes):
    for _ in range(minutes):
        room.step(1, ac)
    return room


class TestRoom(unittest.TestCase):
    def test_start_values(self):
        self.assertEqual(Room().sensors(), {"temp_half": 58, "hum": 80, "outdoor_half": 64})
        self.assertEqual(Room({"temp": 25, "outdoor": -3.2}).sensors()["outdoor_half"], -6)
        self.assertEqual(ROOM_DEFAULTS["temp"], 29.0)  # overriding didn't touch the defaults

    def test_cool_ten_minutes_matches_real_run(self):
        r = run(Room(), COOL, 10)
        self.assertEqual(r.sensors()["temp_half"], 54)
        self.assertTrue(68 <= r.sensors()["hum"] <= 71, r.hum)

    def test_cool_stops_near_target(self):
        r = run(Room(), t.ACState(True, t.MODE_COOL, 270, t.FAN_AUTO), 180)
        self.assertTrue(27.0 <= r.temp < 28.0, r.temp)

    def test_dry_lowers_hum_more_than_temp(self):
        r = run(Room({"outdoor": 29.0}), DRY, 10)
        self.assertTrue(80 - r.hum > 5 * (29 - r.temp) > 0, (r.temp, r.hum))

    def test_higher_fan_cools_faster(self):
        temps = [run(Room(), t.ACState(True, t.MODE_COOL, 200, fan), 10).temp for fan in ("quiet", "1", "3", "5")]
        self.assertEqual(temps, sorted(temps, reverse=True))
        self.assertTrue(temps[0] > temps[-1])

    def test_fan_and_off_drift_to_outdoor(self):
        for ac in (FAN, OFF, None):
            r = run(Room({"temp": 24.0, "outdoor": 32.0}), ac, 60)
            self.assertTrue(24.0 < r.temp < 32.0, r.temp)

    def test_coil_moisture_comes_back(self):
        r = run(Room(), COOL, 10)
        low = r.hum
        self.assertTrue(r.coil > 4)
        run(r, FAN, 5)
        self.assertTrue(r.hum - low >= 5, (low, r.hum))
        self.assertEqual(r.coil, 0)
        # Off gives it back more slowly.
        r = run(Room(), COOL, 10)
        coil = r.coil
        run(r, OFF, 5)
        self.assertTrue(0 < r.coil < coil)

    def test_other_modes_act_like_fan(self):
        for mode in (t.MODE_HEAT, t.MODE_AUTO, t.MODE_UNKNOWN):
            a = run(run(Room(), COOL, 10), FAN, 5)
            b = run(run(Room(), COOL, 10), t.ACState(True, mode, 220, "3"), 5)
            self.assertEqual((a.temp, a.hum), (b.temp, b.hum))

    def test_long_step_equals_minute_steps(self):
        a = Room()
        a.step(10, COOL)
        b = run(Room(), COOL, 10)
        self.assertAlmostEqual(a.temp, b.temp)
        self.assertAlmostEqual(a.hum, b.hum)
        a.step(0, COOL)
        a.step(-1, COOL)
        self.assertAlmostEqual(a.temp, b.temp)

    def test_rounding_and_clamps(self):
        r = Room()
        r.set(temp=26.5, hum=104.0)
        self.assertEqual(r.sensors()["temp_half"], 53)
        self.assertEqual(r.sensors()["hum"], 100)
        r.set(temp=26.24, hum=-1)
        self.assertEqual((r.sensors()["temp_half"], r.sensors()["hum"]), (52, 0))
        r.set(temp=-2.5)
        self.assertEqual(r.sensors()["temp_half"], -5)
        r = run(Room({"hum": 2.0, "hum_ambient": 0.0}), COOL, 10)
        self.assertEqual(r.hum, 0.0)
