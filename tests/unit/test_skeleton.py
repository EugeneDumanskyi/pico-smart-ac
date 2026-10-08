# SPDX-License-Identifier: GPL-3.0-only
# Smoke test: the right runtime, the package imports, and ticks arithmetic survives the wrap.
import sys
import time
import unittest


class TestSkeleton(unittest.TestCase):
    def test_runtime_is_micropython_1_29(self):
        self.assertEqual(sys.implementation.name, "micropython")
        self.assertEqual(sys.implementation.version[:2], (1, 29))

    def test_core_imports(self):
        import smartac.core  # noqa: F401

    def test_ticks_diff_across_wrap(self):
        before = time.ticks_add(0, -5)  # just below the wrap point
        after = time.ticks_add(before, 10)
        self.assertTrue(after < before)  # the raw values wrapped...
        self.assertEqual(time.ticks_diff(after, before), 10)  # ...ticks_diff does not care
        self.assertEqual(time.ticks_diff(before, after), -10)
