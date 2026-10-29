# SPDX-License-Identifier: GPL-3.0-only
import time
import unittest

from smartac.core import clock as c

MIN = 60 * 1000
H = 60 * MIN
T0 = "2026-10-28T08:49:01Z"
W0 = c.parse(T0)


def op(text, kind=100):
    return (text, kind)


def at(wall):
    # Wall seconds -> an AC time text on 2026-10-28 (tests stay inside that day).
    return "2026-10-28T%sZ" % c.hms(wall)


class TestParse(unittest.TestCase):
    def test_dates(self):
        self.assertEqual(c.days(2000, 1, 1), 0)
        self.assertEqual(c.days(2000, 3, 1), 60)  # 2000 is a leap year
        self.assertEqual(c.days(2001, 1, 1), 366)
        self.assertEqual(c.days(2026, 10, 28) - c.days(2026, 10, 27), 1)
        self.assertEqual(c.days(2027, 1, 1) - c.days(2026, 12, 31), 1)
        self.assertEqual(c.days(2028, 3, 1) - c.days(2028, 2, 28), 2)

    def test_parse(self):
        self.assertEqual(W0 % c.DAY_S, 8 * 3600 + 49 * 60 + 1)
        self.assertEqual(W0 // c.DAY_S, c.days(2026, 10, 28))
        self.assertEqual(c.hms(W0), "08:49:01")
        self.assertEqual(c.parse("2026-10-29T00:00:00Z") - c.parse("2026-10-28T23:59:59Z"), 1)

    def test_rejects(self):
        for text in (None, 5, "", "2026-10-28T08:49:01", "2026-10-28 08:49:01Z", "2026-13-28T08:49:01Z",
                     "2026-02-30T08:49:01Z", "2026-10-28T24:00:00Z", "2026-10-28T08:60:01Z", "2026-1a-28T08:49:01Z",
                     "2000-01-01T00:00:00Z", "2026-10-28T08:49:01Z "):
            self.assertIsNone(c.parse(text), text)


class TestClock(unittest.TestCase):
    def test_first_value_is_only_a_baseline(self):
        k = c.Clock()
        self.assertIsNone(k.observe(0, op(T0)))
        self.assertIsNone(k.observe(MIN, op(T0)))
        self.assertIsNone(k.minute(MIN))
        self.assertEqual(k.status(MIN), {"time": None, "source": None})

    def test_a_change_sets_the_clock(self):
        k = c.Clock()
        k.observe(0, op(T0))
        msg = k.observe(MIN, op(at(W0 + 600)))
        self.assertEqual(msg, "AC time 08:59:01 (type 100, clock set)")
        self.assertEqual(k.minute(MIN), 8 * 60 + 59)
        self.assertEqual(k.minute(MIN + 31 * MIN), 9 * 60 + 30)
        self.assertEqual(k.status(MIN), {"time": "08:59", "source": c.AC})
        self.assertIsNone(k.observe(2 * MIN, op(at(W0 + 600))))  # unchanged: no sync

    def test_later_syncs_report_the_drift(self):
        k = c.Clock()
        k.observe(0, op(T0))
        k.observe(0, op(at(W0 + 1)))
        msg = k.observe(30 * MIN, op(at(W0 + 1 + 30 * 60 - 4), 99))
        self.assertEqual(msg, "AC time 09:18:58 (type 99, clock was +4 s)")
        self.assertEqual(k.now_s(30 * MIN), W0 + 1 + 30 * 60 - 4)

    def test_no_last_op_and_bad_text(self):
        k = c.Clock()
        self.assertIsNone(k.observe(0, None))
        k.observe(0, op(T0))
        self.assertEqual(k.observe(MIN, op("garbage")), "AC time unreadable: 'garbage'")
        self.assertIsNone(k.now_s(MIN))

    def test_a_jump_makes_the_time_unknown_until_a_second_sync_agrees(self):
        k = c.Clock()
        k.observe(0, op(T0))
        k.observe(0, op(at(W0 + 1)))
        msg = k.observe(MIN, op(at(W0 + 1 + 60 + 3 * 3600)))
        self.assertEqual(msg, "AC time 11:50:02 (type 100, +3.0 h off; time unknown)")
        self.assertIsNone(k.minute(MIN))
        # Another that disagrees with the suspect: still unknown.
        msg = k.observe(2 * MIN, op(at(W0 + 2 * 60)))
        self.assertEqual(msg, "AC time 08:51:01 (type 100, disagrees with the last; time unknown)")
        self.assertIsNone(k.minute(2 * MIN))
        # One that agrees with that suspect: taken.
        msg = k.observe(12 * MIN, op(at(W0 + 12 * 60 + 5)))
        self.assertEqual(msg, "AC time 09:01:06 (type 100, clock set)")
        self.assertEqual(k.now_s(12 * MIN), W0 + 12 * 60 + 5)

    def test_small_moves_are_taken(self):
        k = c.Clock()
        k.observe(0, op(T0))
        k.observe(0, op(at(W0 + 1)))
        msg = k.observe(MIN, op(at(W0 + 61 + 3600)))
        self.assertEqual(msg, "AC time 09:50:02 (type 100, clock was -60 min)")

    def test_phone_until_the_ac_syncs(self):
        k = c.Clock()
        self.assertEqual(k.phone(0, W0), ("phone time 08:49:01 (clock set)", None))
        self.assertEqual(k.status(0), {"time": "08:49", "source": c.PHONE})
        self.assertEqual(k.phone(MIN, W0 + 70), (None, 10))  # close enough: no event
        self.assertEqual(k.phone(MIN, W0 + 360), ("phone time 08:55:01 (clock was -5 min)", 300))
        k.observe(MIN, op(T0))
        k.observe(2 * MIN, op(at(W0 + 400)))
        self.assertEqual(k.source, c.AC)
        self.assertEqual(k.phone(2 * MIN, W0 + 400 + 600), (None, 600))  # only compared
        self.assertEqual(k.now_s(2 * MIN), W0 + 400)

    def test_wrap_and_rebase(self):
        k = c.Clock()
        start = time.ticks_add(0, -MIN)
        k.observe(start, op(T0))
        k.observe(start, op(at(W0 + 1)))
        now = start
        for _ in range(10 * 24):  # ten days of hourly reads, across the ticks wrap
            now = time.ticks_add(now, H)
            k.now_s(now)
        self.assertEqual(k.now_s(now), W0 + 1 + 10 * c.DAY_S)
        self.assertEqual(k.minute(now), 8 * 60 + 49)

    def test_gap_text(self):
        self.assertEqual(c._gap(0), "+0 s")
        self.assertEqual(c._gap(-119), "-119 s")
        self.assertEqual(c._gap(150), "+2 min")
        self.assertEqual(c._gap(-7200), "-2.0 h")
        self.assertEqual(c._gap(9000), "+2.5 h")
