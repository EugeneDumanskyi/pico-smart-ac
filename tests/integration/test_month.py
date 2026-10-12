# SPDX-License-Identifier: GPL-3.0-only
# The month run (sim/harness.py), shortened to 3 days with a fixed seed: every invariant, no coverage
# requirement. The 30-day run with coverage is `make month`.
import unittest

from sim import harness, scenario


class TestMonth(unittest.TestCase):
    def test_three_days(self):
        try:
            counts = harness.run(seed=1, days=3)
        except harness.Violation as e:
            self.fail(str(e))
        self.assertTrue(counts["cycles"] > 0)
        self.assertTrue(counts["wraps"] > 0)

    def test_scenario_is_reproducible(self):
        a = scenario.generate(7, 30)
        b = scenario.generate(7, 30)
        self.assertEqual(a.lines(), b.lines())
        self.assertNotEqual(a.lines(), scenario.generate(8, 30).lines())
        quiet = a.end_min - scenario.TAIL_MIN
        self.assertEqual([e[1] for e in a.script if e[0] >= quiet and e[1] != "set_room"], ["remote"])
