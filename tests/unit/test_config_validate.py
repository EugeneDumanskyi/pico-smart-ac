# SPDX-License-Identifier: GPL-3.0-only
import unittest

from smartac.core import config


def profiles():
    return config.default_profiles()


def first_rule(doc):
    return doc["profiles"]["day"]["rules"][0]


def paths(items):
    return [e["path"] for e in items]


class ProfileCase(unittest.TestCase):
    def check(self, doc, hard_cap_min=120):
        self.errors, self.warnings = config.validate_profiles(doc, hard_cap_min)
        return paths(self.errors)

    def assertRejects(self, doc, path, hard_cap_min=120):
        got = self.check(doc, hard_cap_min)
        self.assertIn(path, got)
        return self.errors[got.index(path)]["msg"]


class TestDefaultsAreValid(ProfileCase):
    def test_profiles(self):
        self.assertEqual(config.validate_profiles(profiles()), ([], []))

    def test_every_active_choice(self):
        for active in ("off", "day", "night", "away"):
            doc = profiles()
            doc["active"] = active
            self.assertEqual(self.check(doc), [])

    def test_settings(self):
        self.assertEqual(config.validate_settings(config.default_settings(), profiles()), ([], []))
        self.assertEqual(config.validate_settings(config.default_settings("192.0.2.10")), ([], []))


class TestTopLevel(ProfileCase):
    def test_not_an_object(self):
        for doc in (None, [], "x", 3):
            self.assertEqual(self.check(doc), ["$"])

    def test_missing_and_unknown_fields(self):
        doc = profiles()
        del doc["active"]
        doc["extra"] = 1
        got = self.check(doc)
        self.assertIn("active", got)
        self.assertIn("extra", got)

    def test_version(self):
        doc = profiles()
        doc["version"] = 2
        self.assertRejects(doc, "version")

    def test_active_must_exist(self):
        doc = profiles()
        doc["active"] = "evening"
        self.assertRejects(doc, "active")

    def test_off_is_reserved(self):
        doc = profiles()
        doc["profiles"]["off"] = doc["profiles"]["day"]
        self.assertRejects(doc, "profiles.off")

    def test_profile_names_and_count(self):
        doc = profiles()
        doc["profiles"]["Day-2"] = doc["profiles"]["day"]
        self.assertRejects(doc, "profiles.Day-2")
        doc = profiles()
        for i in range(8):
            doc["profiles"]["p%d" % i] = profiles()["profiles"]["away"]
        self.assertRejects(doc, "profiles")

    def test_label(self):
        doc = profiles()
        doc["profiles"]["day"]["label"] = "x" * 33
        self.assertRejects(doc, "profiles.day.label")
        del doc["profiles"]["day"]["label"]
        self.assertEqual(self.check(doc), [])

    def test_document_size(self):
        doc = profiles()
        doc["profiles"]["day"]["label"] = "x" * 32
        for i in range(7):
            doc["profiles"]["p%d" % i] = {"label": "y" * 32, "rules": [first_rule(profiles())] * 8}
        msg = self.assertRejects(doc, "$")
        self.assertIn("16 KB", msg)


class TestRules(ProfileCase):
    def test_rule_count(self):
        doc = profiles()
        doc["profiles"]["day"]["rules"] = []
        self.assertRejects(doc, "profiles.day.rules")
        doc["profiles"]["day"]["rules"] = [first_rule(profiles())] * 9
        self.assertRejects(doc, "profiles.day.rules")

    def test_duplicate_rule_name(self):
        doc = profiles()
        doc["profiles"]["day"]["rules"][1]["name"] = "too_hot"
        self.assertRejects(doc, "profiles.day.rules[1].name")

    def test_same_rule_name_in_two_profiles_is_fine(self):
        self.assertEqual(self.check(profiles()), [])  # "humid" is in day and night

    def test_rule_name(self):
        for bad in ("", "x" * 25, "Too Hot", 5, None):
            doc = profiles()
            first_rule(doc)["name"] = bad
            self.assertRejects(doc, "profiles.day.rules[0].name")

    def test_ranges(self):
        cases = (
            ("confirm_reads", (0, 11, 1.5, True, "2")),
            ("cooldown_min", (-1, 481)),
            ("min_interval_min", (-1, 1441)),
        )
        for key, bads in cases:
            for bad in bads:
                doc = profiles()
                first_rule(doc)[key] = bad
                self.assertRejects(doc, "profiles.day.rules[0].%s" % key)

    def test_range_ends_are_allowed(self):
        doc = profiles()
        r = first_rule(doc)
        r.update({"confirm_reads": 10, "cooldown_min": 480, "min_interval_min": 1440})
        self.assertEqual(self.check(doc), [])

    def test_unknown_rule_field(self):
        doc = profiles()
        first_rule(doc)["cooldown"] = 5
        self.assertRejects(doc, "profiles.day.rules[0].cooldown")


class TestConditions(ProfileCase):
    W = "profiles.day.rules[0].when"

    def test_match(self):
        doc = profiles()
        first_rule(doc)["when"]["match"] = "xor"
        self.assertRejects(doc, self.W + ".match")

    def test_condition_count(self):
        doc = profiles()
        first_rule(doc)["when"]["conditions"] = []
        self.assertRejects(doc, self.W + ".conditions")
        c = {"sensor": "temp", "op": ">", "value": 29}
        first_rule(doc)["when"]["conditions"] = [c] * 5
        self.assertRejects(doc, self.W + ".conditions")

    def test_sensor_and_op(self):
        doc = profiles()
        first_rule(doc)["when"]["conditions"][0].update({"sensor": "dew_point", "op": "=="})
        got = self.check(doc)
        self.assertIn(self.W + ".conditions[0].sensor", got)
        self.assertIn(self.W + ".conditions[0].op", got)

    def test_value_ranges(self):
        cases = (("temp", 9.5, 45.5), ("outdoor_temp", 9, 46), ("hum", 19, 96))
        for sensor, low, high in cases:
            for bad in (low, high, "29", None):
                doc = profiles()
                first_rule(doc)["when"]["conditions"][0].update({"sensor": sensor, "value": bad})
                self.assertRejects(doc, self.W + ".conditions[0].value")

    def test_value_may_be_fractional(self):
        doc = profiles()
        first_rule(doc)["when"]["conditions"][0]["value"] = 28.5
        self.assertEqual(self.check(doc), [])


class TestPhases(ProfileCase):
    P = "profiles.day.rules[0].phases"

    def phase(self, doc, i=0):
        return first_rule(doc)["phases"][i]

    def test_phase_count(self):
        doc = profiles()
        first_rule(doc)["phases"] = []
        self.assertRejects(doc, self.P)
        first_rule(doc)["phases"] = [{"mode": "fan", "fan": "3", "max_min": 10, "until": None}] * 6
        self.assertRejects(doc, self.P)

    def test_mode_and_fan(self):
        for key, bad in (("mode", "heat"), ("mode", "auto"), ("fan", "6"), ("fan", 3)):
            doc = profiles()
            self.phase(doc, 1)[key] = bad
            self.assertRejects(doc, self.P + "[1].%s" % key)

    def test_max_min(self):
        for bad in (0, 121, 10.0):
            doc = profiles()
            self.phase(doc, 1)["max_min"] = bad
            self.assertRejects(doc, self.P + "[1].max_min")

    def test_target_c(self):
        for bad in (17.5, 30.5, 26.3, "26", None):
            doc = profiles()
            self.phase(doc)["target_c"] = bad
            self.assertRejects(doc, self.P + "[0].target_c")
        doc = profiles()
        del self.phase(doc)["target_c"]
        self.assertRejects(doc, self.P + "[0].target_c")
        for good in (18, 25.5, 30):
            doc = profiles()
            self.phase(doc)["target_c"] = good
            self.assertEqual(self.check(doc), [])

    def test_target_only_for_cool(self):
        doc = profiles()
        self.phase(doc, 1)["target_c"] = 26
        self.assertRejects(doc, self.P + "[1].target_c")

    def test_until_is_optional(self):
        doc = profiles()
        del self.phase(doc)["until"]
        self.assertEqual(self.check(doc), [])

    def test_until_is_checked_like_a_condition(self):
        doc = profiles()
        self.phase(doc)["until"] = {"sensor": "temp", "op": "<=", "value": 5}
        self.assertRejects(doc, self.P + "[0].until.value")

    def test_sum_within_hard_cap(self):
        doc = profiles()
        self.phase(doc)["max_min"] = 101  # 101 + 20
        msg = self.assertRejects(doc, self.P)
        self.assertIn("121", msg)
        self.assertEqual(self.check(doc, hard_cap_min=121), [])
        self.assertRejects(profiles(), "profiles.day.rules[1].phases", hard_cap_min=60)  # humid: 80 min


class TestWarnings(ProfileCase):
    P = "profiles.day.rules[0].phases"

    def test_goal_that_moves_the_wrong_way(self):
        for until in (
            {"sensor": "temp", "op": ">=", "value": 30},
            {"sensor": "outdoor_temp", "op": "<", "value": 20},
        ):
            doc = profiles()
            first_rule(doc)["phases"][0]["until"] = until
            self.assertEqual(self.check(doc), [])
            self.assertEqual(paths(self.warnings), [self.P + "[0].until"])

    def test_goal_on_fan_phase(self):
        doc = profiles()
        first_rule(doc)["phases"][1]["until"] = {"sensor": "hum", "op": "<=", "value": 60}
        self.assertEqual(self.check(doc), [])
        self.assertEqual(paths(self.warnings), [self.P + "[1].until"])

    def test_cool_goal_on_humidity_is_fine(self):
        doc = profiles()
        first_rule(doc)["phases"][0]["until"] = {"sensor": "hum", "op": "<", "value": 60}
        self.check(doc)
        self.assertEqual(self.warnings, [])

    def test_no_final_fan_phase(self):
        doc = profiles()
        first_rule(doc)["phases"].pop()
        self.assertEqual(self.check(doc), [])
        self.assertEqual(paths(self.warnings), [self.P])

    def test_fan_only_rule_needs_no_final_fan(self):
        doc = profiles()
        first_rule(doc)["phases"] = [{"mode": "fan", "fan": "3", "max_min": 30, "until": None}]
        self.check(doc)
        self.assertEqual(self.warnings, [])


class TestBrokenShapes(ProfileCase):
    def test_wrong_types_do_not_raise(self):
        cases = (
            {"version": 1, "active": "off", "profiles": []},
            {"version": 1, "active": "off", "profiles": {"day": None}},
            {"version": 1, "active": "off", "profiles": {"day": {"rules": None}}},
            {"version": 1, "active": "off", "profiles": {"day": {"rules": [None, 3]}}},
            {"version": 1, "active": "off", "profiles": {"day": {"rules": [{"when": [], "phases": [None]}]}}},
            {"version": 1, "active": "off", "profiles": {"day": {"rules": [{"when": {"conditions": [7]}}]}}},
            {"version": 1, "active": ["day"], "profiles": {}},
        )
        for doc in cases:
            self.assertTrue(self.check(doc), doc)

    def test_errors_carry_messages(self):
        doc = profiles()
        first_rule(doc)["confirm_reads"] = 0
        self.check(doc)
        want = {"path": "profiles.day.rules[0].confirm_reads", "msg": "must be a whole number 1–10"}
        self.assertEqual(self.errors, [want])


class TestSettings(unittest.TestCase):
    def check(self, **changes):
        s = config.default_settings()
        s.update(changes)
        return paths(config.validate_settings(s)[0])

    def test_int_ranges(self):
        cases = (
            ("poll_s", 29, 301),
            ("hard_cap_min", 29, 481),
            ("manual_hold_min", 9, 481),
            ("manual_release_off_min", 4, 241),
            ("finish_fan_min", -1, 61),
        )
        for key, low, high in cases:
            for bad in (low, high, 60.0, None, False):
                self.assertEqual(self.check(**{key: bad}), [key])

    def test_ac_ip(self):
        for good in (None, "192.0.2.10", "0.0.0.0", "255.255.255.255"):
            self.assertEqual(self.check(ac_ip=good), [])
        for bad in ("", "192.0.2", "192.0.2.256", "192.0.2.x", "192.0.2.10 ", "ac.local", 3):
            self.assertEqual(self.check(ac_ip=bad), ["ac_ip"])

    def test_dry_run(self):
        self.assertEqual(self.check(dry_run=True), [])
        self.assertEqual(self.check(dry_run=1), ["dry_run"])

    def test_sensor_limits(self):
        self.assertEqual(self.check(sensor_limits={"temp": [15.5, 35], "hum": [0, 100]}), [])
        for bad in ([20, 20], [30, 20], [-1, 30], [10, 61], [10], "10-45", [10, "45"]):
            self.assertEqual(self.check(sensor_limits={"temp": bad, "hum": [5, 100]}), ["sensor_limits.temp"])
        self.assertEqual(self.check(sensor_limits={"temp": [10, 45]}), ["sensor_limits.hum"])
        self.assertEqual(self.check(sensor_limits=None), ["sensor_limits"])

    def test_unknown_and_missing(self):
        s = config.default_settings()
        del s["poll_s"]
        s["api_token"] = "x"
        got = paths(config.validate_settings(s)[0])
        self.assertIn("poll_s", got)
        self.assertIn("api_token", got)
        self.assertEqual(paths(config.validate_settings(None)[0]), ["$"])

    def test_hard_cap_against_current_profiles(self):
        s = config.default_settings()
        s["hard_cap_min"] = 60
        errors = config.validate_settings(s, profiles())[0]
        self.assertEqual(paths(errors), ["hard_cap_min"])  # only day.humid (80 min); 60 still fits
        self.assertIn("profiles.day.rules[1]", errors[0]["msg"])
