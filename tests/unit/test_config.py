# SPDX-License-Identifier: GPL-3.0-only
import json
import unittest

from smartac.core import config
from smartac.core import daikin_codec as codec
from smartac.core import types as t

NAME_CHARS = "abcdefghijklmnopqrstuvwxyz0123456789_"


def rules_of(name):
    return config.default_profiles()["profiles"][name]["rules"]


def summary(rule):
    # Compact view of a rule to compare with the tables in docs/idea.md.
    c = rule["when"]["conditions"][0]
    phases = []
    for p in rule["phases"]:
        u = p["until"]
        goals = [(g["sensor"], g["op"], g["value"]) for g in (u if isinstance(u, list) else [u] if u else [])]
        phases.append((p["mode"], p.get("target_c"), p["fan"], p["max_min"], goals))
    return (rule["name"], (c["sensor"], c["op"], c["value"]), phases, rule["cooldown_min"], rule["min_interval_min"])


class TestDefaultProfiles(unittest.TestCase):
    def test_top_level(self):
        doc = config.default_profiles()
        self.assertEqual(doc["version"], 1)
        self.assertEqual(doc["active"], config.OFF)
        self.assertEqual(sorted(doc["profiles"]), ["away", "day", "night"])
        self.assertFalse(config.OFF in doc["profiles"])

    def test_day(self):
        self.assertEqual(
            [summary(r) for r in rules_of("day")],
            [
                (
                    "too_hot",
                    ("temp", ">", 29),
                    [("cool", 26, "3", 30, [("temp", "<=", 26)]), ("fan", None, "3", 20, [])],
                    20,
                    0,
                ),
                (
                    "humid",
                    ("hum", ">", 75),
                    [("cool", 26, "3", 40, [("hum", "<=", 65), ("temp", "<=", 25)]), ("fan", None, "3", 20, [])],
                    30,
                    0,
                ),
            ],
        )

    def test_night(self):
        self.assertEqual(
            [summary(r) for r in rules_of("night")],
            [
                (
                    "warm",
                    ("temp", ">", 27),
                    [("cool", 25, "3", 20, [("temp", "<=", 25)]), ("fan", None, "3", 15, [])],
                    30,
                    0,
                ),
                (
                    "humid",
                    ("hum", ">", 75),
                    [("dry", None, "3", 45, [("hum", "<=", 65)]), ("fan", None, "3", 15, [])],
                    45,
                    0,
                ),
            ],
        )

    def test_away(self):
        self.assertEqual(
            [summary(r) for r in rules_of("away")],
            [("damp", ("hum", ">", 80), [("dry", None, "3", 30, []), ("fan", None, "3", 20, [])], 0, 240)],
        )

    def test_fresh_copy_each_call(self):
        a = config.default_profiles()
        a["profiles"]["day"]["rules"][0]["phases"][0]["max_min"] = 99
        a["active"] = "day"
        b = config.default_profiles()
        self.assertEqual(b["profiles"]["day"]["rules"][0]["phases"][0]["max_min"], 30)
        self.assertEqual(b["active"], config.OFF)

    def test_json_round_trip(self):
        doc = config.default_profiles()
        self.assertEqual(json.loads(json.dumps(doc)), doc)
        self.assertTrue(len(json.dumps(doc)) < 16 * 1024)


class TestDefaultsWithinLimits(unittest.TestCase):
    # The defaults must pass the validation table in docs/idea.md once validation exists.
    def test_every_rule(self):
        settings = config.default_settings()
        doc = config.default_profiles()
        self.assertLessEqual(len(doc["profiles"]), 10)
        for pname, profile in doc["profiles"].items():
            rules = profile["rules"]
            self.assertTrue(1 <= len(rules) <= 8, pname)
            self.assertTrue(all(ch in NAME_CHARS for ch in pname))
            self.assertEqual(len({r["name"] for r in rules}), len(rules), pname)
            for r in rules:
                self.assertTrue(1 <= len(r["name"]) <= 24 and all(ch in NAME_CHARS for ch in r["name"]))
                self.assertIn(r["when"]["match"], ("all", "any"))
                for c in r["when"]["conditions"]:
                    self.assertIn(c["sensor"], t.SENSORS)
                    self.assertIn(c["op"], (">", ">=", "<", "<="))
                self.assertTrue(1 <= r["confirm_reads"] <= 10)
                self.assertTrue(0 <= r["cooldown_min"] <= 480)
                self.assertTrue(0 <= r["min_interval_min"] <= 1440)
                phases = r["phases"]
                self.assertTrue(1 <= len(phases) <= 5)
                self.assertLessEqual(sum(p["max_min"] for p in phases), settings["hard_cap_min"])
                for p in phases:
                    self.assertIn(p["mode"], (t.MODE_COOL, t.MODE_DRY, t.MODE_FAN))
                    self.assertIn(p["fan"], t.FANS)
                    self.assertTrue(1 <= p["max_min"] <= 120)
                    if p["mode"] == t.MODE_COOL:
                        self.assertTrue(18 <= p["target_c"] <= 30)
                    else:
                        self.assertFalse("target_c" in p)
                self.assertEqual(phases[-1]["mode"], t.MODE_FAN, "%s.%s ends with fan" % (pname, r["name"]))


class TestDefaultSettings(unittest.TestCase):
    def test_values(self):
        self.assertEqual(
            config.default_settings(),
            {
                "version": 1,
                "ac_ip": None,
                "poll_s": 60,
                "hard_cap_min": 120,
                "manual_hold_min": 120,
                "manual_release_off_min": 30,
                "finish_fan_min": 20,
                "sensor_limits": {"temp": [10, 45], "hum": [5, 100]},
                "dry_run": False,
            },
        )

    def test_ac_ip_passed_in(self):
        self.assertEqual(config.default_settings("192.0.2.10")["ac_ip"], "192.0.2.10")

    def test_fresh_copy_each_call(self):
        a = config.default_settings()
        a["sensor_limits"]["temp"][0] = 0
        self.assertEqual(config.default_settings()["sensor_limits"]["temp"], [10, 45])

    def test_sensor_limits_match_codec(self):
        limits = config.default_settings()["sensor_limits"]
        for name, (lo, hi) in codec.DEFAULT_LIMITS.items():
            self.assertEqual(limits[name], [lo, hi])
