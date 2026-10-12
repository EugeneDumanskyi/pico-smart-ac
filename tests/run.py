# SPDX-License-Identifier: GPL-3.0-only
# Test runner for the MicroPython Unix port (docs/decisions/0005-testing.md).
# Usage: micropython tests/run.py [module]   e.g. module = test_skeleton
# Runs every test_*.py in tests/unit, tests/golden and tests/integration; exits 1 on any failure.
import os
import sys

SUITE_DIRS = ("unit", "golden", "integration")


TESTS = __file__.rsplit("/", 1)[0] if "/" in __file__ else "."
sys.path.insert(0, TESTS + "/lib")
sys.path.insert(0, TESTS + "/../src")
sys.path.insert(0, TESTS + "/..")  # the sim package

import unittest  # noqa: E402  (vendored copy, found through the path above)


def _modules(directory):
    try:
        names = sorted(entry[0] for entry in os.ilistdir(directory))
    except OSError:
        return []
    return [name[:-3] for name in names if name.startswith("test_") and name.endswith(".py")]


def main(only=None):
    suite = unittest.TestSuite("smartac")
    broken = 0
    found = 0
    for sub in SUITE_DIRS:
        directory = TESTS + "/" + sub
        for name in _modules(directory):
            if only and name != only:
                continue
            found += 1
            sys.path.insert(0, directory)
            try:
                suite._load_module(__import__(name))
            except Exception as e:
                broken += 1
                print("ERROR importing %s/%s: %r" % (sub, name, e))
                sys.print_exception(e)
            finally:
                sys.path.pop(0)
    if not found:
        print("no test modules found" + (" named " + only if only else ""))
        return 1
    result = unittest.TestRunner().run(suite)
    if broken:
        print("FAILED (%d test modules did not import)" % broken)
    return 0 if result.wasSuccessful() and result.testsRun and not broken else 1


sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else None))
