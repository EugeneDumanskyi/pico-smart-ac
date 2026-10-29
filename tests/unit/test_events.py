# SPDX-License-Identifier: GPL-3.0-only
import json
import time
import unittest

from smartac.core import events as e
from smartac.core import types as t

MIN = 60 * 1000
DAY = 24 * 60 * MIN


def at(ms):
    # A clock reading `ms` after tick 0 (negative: before the wrap).
    return time.ticks_add(0, ms)


class TestRing(unittest.TestCase):
    def test_newest_first_with_limit(self):
        log = e.EventLog(boot=3)
        for n in range(5):
            log.add(at(n * MIN), n * MIN, t.EV_PHASE, "m%d" % n)
        rows = log.export(at(5 * MIN), limit=3)
        self.assertEqual([r["msg"] for r in rows], ["m4", "m3", "m2"])
        self.assertEqual([r["age_s"] for r in rows], [60, 120, 180])
        self.assertEqual(rows[0], {"age_s": 60, "boot": 3, "kind": t.EV_PHASE, "msg": "m4", "temp": None, "hum": None})
        self.assertEqual(len(log.export(at(5 * MIN))), 5)

    def test_empty(self):
        self.assertEqual(e.EventLog(boot=1).export(at(0)), [])

    def test_overflow_keeps_newest(self):
        log = e.EventLog(boot=1)
        for n in range(e.SIZE + 50):
            log.add(at(n * 1000), n * 1000, t.EV_PHASE, str(n))
        rows = log.export(at((e.SIZE + 50) * 1000), limit=1000)
        self.assertEqual(len(rows), e.SIZE)
        self.assertEqual(rows[0]["msg"], str(e.SIZE + 49))
        self.assertEqual(rows[-1]["msg"], "50")
        self.assertEqual(rows[-1]["age_s"], e.SIZE)

    def test_small_ring(self):
        log = e.EventLog(boot=1, size=3)
        for n in range(7):
            log.add(at(n), n, t.EV_PHASE, str(n))
        self.assertEqual([r["msg"] for r in log.export(at(10))], ["6", "5", "4"])

    def test_message_cap(self):
        log = e.EventLog(boot=1)
        line = log.add(at(0), 0, t.EV_MANUAL, "x" * 200)
        self.assertEqual(log.export(at(0))[0]["msg"], "x" * e.MAX_MSG)
        self.assertEqual(json.loads(line)["msg"], "x" * e.MAX_MSG)


class TestFlash(unittest.TestCase):
    def test_kinds(self):
        # The decisions go to flash in every mode; ignored actions stay in RAM.
        flash = (t.EV_CYCLE_END, t.EV_MANUAL, t.EV_TAKEOVER, t.EV_WRITE_FAILED, t.EV_CONFIG_SAVED, t.EV_BOOT,
                 t.EV_CYCLE_START, t.EV_PHASE, t.EV_WOULD, t.EV_HARD_CAP, t.EV_PROFILE, t.EV_UNREACHABLE,
                 t.EV_BLOCKED, t.EV_PAUSE, t.EV_RESUME, t.EV_LOW_MEMORY)
        ram = (t.EV_IGNORED,)
        log = e.EventLog(boot=1)
        for kind in flash:
            self.assertTrue(e.to_flash(kind), kind)
            self.assertIsNotNone(log.add(at(0), 0, kind, "m"), kind)
        for kind in ram:
            self.assertFalse(e.to_flash(kind), kind)
            self.assertIsNone(log.add(at(0), 0, kind, "m"), kind)
        self.assertEqual(len(log.export(at(0), limit=100)), len(flash) + len(ram))
        wide = e.EventLog(boot=1, flash_kinds=e.FLASH_KINDS + (t.EV_IGNORED,))  # sim/supervised.py's set
        self.assertIsNotNone(wide.add(at(0), 0, t.EV_IGNORED, "m"))

    def test_line_round_trip(self):
        log = e.EventLog(boot=7)
        line = log.add(at(5 * MIN), 90500, t.EV_CYCLE_END, "too_hot ended")
        self.assertEqual(
            json.loads(line),
            {"boot": 7, "up_s": 90, "kind": t.EV_CYCLE_END, "msg": "too_hot ended", "temp": None, "hum": None},
        )
        self.assertEqual(
            e.from_line(line),
            {"age_s": None, "boot": 7, "kind": t.EV_CYCLE_END, "msg": "too_hot ended", "temp": None, "hum": None},
        )
        self.assertEqual(e.from_line(e.to_line(2, 0, t.EV_BOOT, "boot 2"))["kind"], t.EV_BOOT)

    def test_reading_round_trip(self):
        line = e.to_line(4, 61000, t.EV_WOULD, "would set dry, fan auto", 285, 81)
        d = json.loads(line)
        self.assertEqual((d["up_s"], d["temp"], d["hum"]), (61, 28.5, 81))
        row = e.from_line(line)
        self.assertEqual((row["temp"], row["hum"], row["msg"]), (28.5, 81, "would set dry, fan auto"))
        self.assertEqual(e.from_line(e.to_line(4, 0, t.EV_WOULD, "m", -15, 5))["temp"], -1.5)

    def test_old_line_without_reading(self):
        # events.log lines written before M3.
        row = e.from_line('{"boot": 3, "up_s": 9, "kind": "manual", "msg": "AC changed by hand"}')
        self.assertEqual(row, {"age_s": None, "boot": 3, "kind": "manual", "msg": "AC changed by hand", "temp": None,
                               "hum": None})

    def test_ring_keeps_readings(self):
        log = e.EventLog(boot=2, size=2)
        log.add(at(0), 0, t.EV_CYCLE_START, "a", 300, 70)
        log.add(at(MIN), MIN, t.EV_WOULD, "b", 295, None)
        log.add(at(2 * MIN), 2 * MIN, t.EV_PHASE, "c")  # overwrites "a": its reading must not stick
        rows = log.export(at(2 * MIN))
        self.assertEqual([(r["msg"], r["temp"], r["hum"]) for r in rows], [("c", None, None), ("b", 29.5, None)])

    def test_corrupt_line(self):
        for line in ('{"boot": 7, "up_s": 9, "kind": "boot", "ms', "", "[]", "null", '{"boot": 7}',
                     '{"boot": "7", "up_s": 0, "kind": "boot", "msg": ""}',
                     '{"boot": 7, "up_s": 0, "kind": "boot", "msg": "", "temp": "28"}',
                     '{"boot": 7, "up_s": 0, "kind": "boot", "msg": "", "hum": true}'):
            self.assertIsNone(e.from_line(line), line)


class TestAges(unittest.TestCase):
    def test_ticks_wrap(self):
        log = e.EventLog(boot=1)
        log.add(at(-3 * MIN), 0, t.EV_PHASE, "before")
        log.add(at(-1 * MIN), 2 * MIN, t.EV_PHASE, "just before")
        rows = log.export(at(2 * MIN))
        self.assertEqual([r["age_s"] for r in rows], [180, 300])

    def test_long_ages_with_daily_prune(self):
        # Ten days, pruned once a day, starting 3 days before the wrap: well past half a Pico ticks period.
        log = e.EventLog(boot=1)
        start = -3 * DAY
        log.add(at(start), 0, t.EV_CYCLE_END, "old")
        for day in range(1, 11):
            log.prune(at(start + day * DAY))
        log.add(at(start + 10 * DAY), 10 * DAY, t.EV_PHASE, "new")
        rows = log.export(at(start + 10 * DAY + MIN))
        self.assertEqual([r["age_s"] for r in rows], [60, (10 * DAY + MIN) // 1000])

    def test_prune_every_minute(self):
        log = e.EventLog(boot=1)
        log.add(at(-MIN), 0, t.EV_PHASE, "m")
        for n in range(0, 2 * DAY, 30 * MIN):
            log.prune(at(n))
        self.assertEqual(log.export(at(2 * DAY))[0]["age_s"], (2 * DAY + MIN) // 1000)

    def test_old_event_without_prune(self):
        log = e.EventLog(boot=1)
        log.add(at(0), 0, t.EV_PHASE, "m")
        self.assertEqual(log.export(at(3 * DAY))[0]["age_s"], 3 * DAY // 1000)
        self.assertEqual(log.export(at(3 * DAY + MIN))[0]["age_s"], (3 * DAY + MIN) // 1000)

    def test_negative_age_is_clamped(self):
        # A stamp "in the future" (from before a missed wrap) counts as age 0 from the first prune.
        log = e.EventLog(boot=1)
        log.add(at(MIN), 0, t.EV_PHASE, "m")
        log.prune(at(0))
        self.assertEqual(log.export(at(MIN))[0]["age_s"], 60)
