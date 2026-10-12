# SPDX-License-Identifier: GPL-3.0-only
# Fixtures are built in the multireq response shape (responses[].pc.pch tree, see docs/DAIKIN_API.md
# and tools/daikin_status.py) with made-up values; no addresses or identifiers.
import unittest

from smartac.core import daikin_codec as codec
from smartac.core import types as t


def leaf(pn, pv):
    return {"pn": pn, "pt": 1, "pv": pv}


def node(pn, *children):
    return {"pn": pn, "pt": 1, "pch": list(children)}


def indoor_root(power="00", mode="0000", e3001=None, room_t="1D", room_h="50"):
    params = [leaf("p_01", mode)]
    for pn, pv in (e3001 or {}).items():
        params.append(leaf(pn, pv))
    return node(
        "e_1002",
        node("e_A002", leaf("p_01", power)),
        node("e_3001", *params),
        node("e_3003", leaf("p_20", "00"), leaf("p_28", "01")),
        node("e_A00B", leaf("p_01", room_t), leaf("p_02", room_h), leaf("p_05", "1234")),
    )


def outdoor_root(out_t="3F00"):
    return node("e_1003", node("e_A00D", leaf("p_01", out_t)), node("e_3002", leaf("p_44", "00")))


def response(indoor, outdoor=None, rsc_in=2000, rsc_out=2000):
    rs = [{"fr": codec.INDOOR, "pc": {"pn": "dgc_status", "pch": [indoor]}, "rsc": rsc_in}]
    if outdoor is not None:
        rs.append({"fr": codec.OUTDOOR, "pc": {"pn": "dgc_status", "pch": [outdoor]}, "rsc": rsc_out})
    return {"responses": rs}


# Cool 26.0 °C (raw 52 = 0x34), fan auto; room 29 °C / 80 %; outdoor 31.5 °C (raw 63).
COOL_E3001 = {"p_02": "34", "p_03": "2C", "p_1D": "30", "p_09": "0A00", "p_0A": "0300", "p_27": "0B00", "p_28": "0500"}


class TestLeInt(unittest.TestCase):
    def test_values(self):
        self.assertEqual(codec.le_int("2E"), 46)
        self.assertEqual(codec.le_int("3F00"), 63)
        self.assertEqual(codec.le_int("0001"), 256)
        self.assertEqual(codec.le_int("ff"), 255)

    def test_signed(self):
        self.assertEqual(codec.le_int("FBFF", signed=True), -5)
        self.assertEqual(codec.le_int("FBFF"), 0xFFFB)
        self.assertEqual(codec.le_int("F6", signed=True), -10)
        self.assertEqual(codec.le_int("7F", signed=True), 127)

    def test_rejects_bad_input(self):
        for bad in ("", "A", "0G", " 1", "+1", None, 12):
            with self.assertRaises(ValueError):
                codec.le_int(bad)


class TestReadRequest(unittest.TestCase):
    def test_reads_indoor_and_outdoor(self):
        tos = [r["to"] for r in codec.READ_REQUEST["requests"]]
        self.assertEqual(tos, [codec.INDOOR + "?filter=pv,pt,md", codec.OUTDOOR + "?filter=pv,pt,md"])
        self.assertTrue(all(r["op"] == 2 for r in codec.READ_REQUEST["requests"]))


class TestDecode(unittest.TestCase):
    def test_cool_full_reading(self):
        r = codec.decode(response(indoor_root("01", "0200", COOL_E3001), outdoor_root()))
        self.assertTrue(r.reachable)
        self.assertEqual(r.ac, t.ACState(True, t.MODE_COOL, 260, t.FAN_AUTO))
        self.assertEqual(r.temp, 290)
        self.assertEqual(r.hum, 80)
        self.assertEqual(r.outdoor_temp, 315)

    def test_per_mode_target_and_fan(self):
        cases = (
            ("0100", t.ACState(True, t.MODE_HEAT, 220, "1")),
            ("0300", t.ACState(True, t.MODE_AUTO, 240, None)),  # p_26 absent
            ("0500", t.ACState(True, t.MODE_DRY, None, t.FAN_QUIET)),
            ("0000", t.ACState(True, t.MODE_FAN, None, "3")),
        )
        for code, want in cases:
            r = codec.decode(response(indoor_root("01", code, COOL_E3001)))
            self.assertEqual(r.ac, want)

    def test_off_keeps_mode(self):
        r = codec.decode(response(indoor_root("00", "0000", COOL_E3001), outdoor_root()))
        self.assertEqual(r.ac, t.ACState(False, t.MODE_FAN, None, "3"))

    def test_half_degree_target(self):
        r = codec.decode(response(indoor_root("01", "0200", {"p_02": "2D", "p_09": "0A00"})))
        self.assertEqual(r.ac.target, 225)

    def test_unknown_mode_code(self):
        r = codec.decode(response(indoor_root("01", "0700", COOL_E3001)))
        self.assertTrue(r.reachable)
        self.assertEqual(r.ac, t.ACState(True, t.MODE_UNKNOWN, None, None))

    def test_unknown_fan_code(self):
        r = codec.decode(response(indoor_root("01", "0200", {"p_02": "34", "p_09": "0900"})))
        self.assertIsNone(r.ac.fan)

    def test_negative_outdoor(self):
        r = codec.decode(response(indoor_root(), outdoor_root("FBFF")))
        self.assertEqual(r.outdoor_temp, -25)

    def test_response_order_does_not_matter(self):
        resp = response(indoor_root("01", "0200", COOL_E3001), outdoor_root())
        resp["responses"].reverse()
        r = codec.decode(resp)
        self.assertEqual(r.ac, t.ACState(True, t.MODE_COOL, 260, t.FAN_AUTO))
        self.assertEqual(r.outdoor_temp, 315)

    def test_missing_outdoor_is_still_reachable(self):
        r = codec.decode(response(indoor_root()))
        self.assertTrue(r.reachable)
        self.assertIsNone(r.outdoor_temp)
        r = codec.decode(response(indoor_root(), outdoor_root(), rsc_out=4000))
        self.assertTrue(r.reachable)
        self.assertIsNone(r.outdoor_temp)


class TestDecodeUnreachable(unittest.TestCase):
    def assertUnreachable(self, resp):
        r = codec.decode(resp)
        self.assertFalse(r.reachable)
        self.assertTrue(r.error)
        return r

    def test_malformed(self):
        for resp in (None, [], {}, {"responses": None}, {"responses": [None, 3]}, {"responses": []}):
            self.assertUnreachable(resp)

    def test_indoor_rsc_error(self):
        r = self.assertUnreachable(response(indoor_root(), outdoor_root(), rsc_in=4000))
        self.assertIn("4000", r.error)

    def test_no_indoor_root(self):
        self.assertUnreachable(response(outdoor_root()))

    def test_bad_or_missing_power(self):
        self.assertUnreachable(response(indoor_root(power="02")))
        root = indoor_root()
        root["pch"] = [c for c in root["pch"] if c["pn"] != "e_A002"]
        self.assertUnreachable(response(root))

    def test_missing_mode(self):
        root = indoor_root()
        root["pch"] = [c for c in root["pch"] if c["pn"] != "e_3001"]
        self.assertUnreachable(response(root))

    def test_broken_tree(self):
        root = indoor_root()
        root["pch"][0]["pch"] = "oops"
        self.assertUnreachable(response(root))


class TestRangeChecks(unittest.TestCase):
    def decode_room(self, room_t="1D", room_h="50", limits=None):
        return codec.decode(response(indoor_root(room_t=room_t, room_h=room_h)), limits)

    def test_default_limits_inclusive(self):
        r = self.decode_room("0A", "05")  # 10 °C, 5 %
        self.assertEqual((r.temp, r.hum), (100, 5))
        r = self.decode_room("2D", "64")  # 45 °C, 100 %
        self.assertEqual((r.temp, r.hum), (450, 100))

    def test_out_of_range_becomes_none(self):
        r = self.decode_room("09", "04")
        self.assertTrue(r.reachable)
        self.assertEqual((r.temp, r.hum), (None, None))
        r = self.decode_room("2E", "65")  # 46 °C, 101 %
        self.assertEqual((r.temp, r.hum), (None, None))
        r = self.decode_room("FE")  # -2 °C, signed
        self.assertIsNone(r.temp)

    def test_garbage_sensor_becomes_none(self):
        r = self.decode_room("ZZ", "")
        self.assertTrue(r.reachable)
        self.assertEqual((r.temp, r.hum), (None, None))

    def test_custom_limits_from_settings(self):
        limits = {"temp": [15, 35], "hum": [20, 90]}
        r = self.decode_room("0E", "5A", limits)  # 14 °C, 90 %
        self.assertEqual((r.temp, r.hum), (None, 90))
        r = self.decode_room("1D", "50", {"temp": [15, 35]})  # hum falls back to default
        self.assertEqual((r.temp, r.hum), (290, 80))

    def test_outdoor_limits(self):
        r = codec.decode(response(indoor_root(), outdoor_root("7900")))  # 60.5 °C
        self.assertIsNone(r.outdoor_temp)
        r = codec.decode(response(indoor_root(), outdoor_root("C4FF")))  # -30.0 °C
        self.assertEqual(r.outdoor_temp, -300)

    def test_target_out_of_range_becomes_none(self):
        r = codec.decode(response(indoor_root("01", "0200", {"p_02": "42", "p_09": "0A00"})))  # 33 °C
        self.assertIsNone(r.ac.target)
        self.assertEqual(r.ac.mode, t.MODE_COOL)
