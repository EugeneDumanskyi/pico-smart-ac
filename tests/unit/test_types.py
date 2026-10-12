# SPDX-License-Identifier: GPL-3.0-only
import time
import unittest

from smartac.core import types as t


class TestACState(unittest.TestCase):
    def test_equal_when_all_fields_match(self):
        self.assertEqual(t.ACState(True, t.MODE_COOL, 260, t.FAN_AUTO), t.ACState(True, t.MODE_COOL, 260, t.FAN_AUTO))

    def test_any_field_differs(self):
        base = t.ACState(True, t.MODE_COOL, 260, t.FAN_AUTO)
        for other in (
            t.ACState(False, t.MODE_COOL, 260, t.FAN_AUTO),
            t.ACState(True, t.MODE_DRY, 260, t.FAN_AUTO),
            t.ACState(True, t.MODE_COOL, 265, t.FAN_AUTO),
            t.ACState(True, t.MODE_COOL, 260, t.FAN_QUIET),
        ):
            self.assertNotEqual(base, other)
            self.assertTrue(base != other)

    def test_not_equal_to_other_types(self):
        self.assertNotEqual(t.ACState(False, t.MODE_FAN), None)


class TestReading(unittest.TestCase):
    def test_reachable_reading(self):
        r = t.Reading(t.ACState(False, t.MODE_FAN), temp=290, hum=80, outdoor_temp=315)
        self.assertTrue(r.reachable)
        self.assertIsNone(r.error)
        self.assertEqual(r.sensor(t.SENSOR_TEMP), 290)
        self.assertEqual(r.sensor(t.SENSOR_HUM), 80)
        self.assertEqual(r.sensor(t.SENSOR_OUTDOOR), 315)
        self.assertIsNone(r.sensor("dew_point"))

    def test_unreachable(self):
        r = t.Reading.unreachable("timeout")
        self.assertFalse(r.reachable)
        self.assertEqual(r.error, "timeout")
        for name in t.SENSORS:
            self.assertIsNone(r.sensor(name))


class TestUnits(unittest.TestCase):
    def test_tenths_from_int_and_float(self):
        self.assertEqual(t.tenths(26), 260)
        self.assertEqual(t.tenths(26.5), 265)
        self.assertEqual(t.tenths(31.5), 315)
        self.assertEqual(t.tenths(-2.5), -25)
        self.assertIsInstance(t.tenths(26.5), int)

    def test_tenths_float_noise(self):
        self.assertEqual(t.tenths(0.1 + 0.2), 3)
        self.assertEqual(t.tenths(28.999999), 290)

    def test_ticks_since_across_wrap(self):
        then = time.ticks_add(0, -1000)
        now = time.ticks_add(then, 61000)
        self.assertEqual(t.ticks_since(now, then), 61000)
        self.assertEqual(t.ticks_since(then, now), -61000)
