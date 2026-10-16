# SPDX-License-Identifier: GPL-3.0-only
# FakeDaikin without sockets: reads, writes, per-mode fields, script events, the ticks wrap.
import time
import unittest

from sim.clock import FakeClock
from sim.device import FakeDaikin
from smartac.core import daikin_codec as codec
from smartac.core import types as t

COOL = t.ACState(True, t.MODE_COOL, 245, "4")
OFF_FAN = t.ACState(False, t.MODE_FAN, None, "3")  # the start state


def read(dev):
    return codec.decode(dev.handle(codec.READ_REQUEST))


def write(dev, state):
    return codec.write_result(dev.handle(codec.write_request(state)))


def streamer_write():
    body = codec.write_request(t.ACState(True, t.MODE_FAN))
    body["requests"][0]["pc"]["pch"][0]["pch"].append({"pn": "e_3003", "pch": [{"pn": "p_20", "pv": "01"}]})
    return body


class TestReads(unittest.TestCase):
    def test_start_state(self):
        r = read(FakeDaikin(FakeClock()))
        self.assertEqual(r.ac, OFF_FAN)
        self.assertEqual((r.temp, r.hum, r.outdoor_temp), (290, 80, 320))

    def test_response_shape(self):
        resp = FakeDaikin(FakeClock()).handle({"requests": [{"op": 2, "to": codec.OUTDOOR + "?filter=pv"}]})
        self.assertEqual(len(resp["responses"]), 1)
        out = resp["responses"][0]
        self.assertEqual((out["fr"], out["rsc"]), (codec.OUTDOOR, 2000))
        self.assertEqual(out["pc"]["pch"][0]["pn"], "e_1003")

    def test_streamer_and_mold_proof_read_as_off(self):
        resp = FakeDaikin(FakeClock()).handle(codec.READ_REQUEST)
        root = resp["responses"][0]["pc"]["pch"][0]
        e3003 = next(c for c in root["pch"] if c["pn"] == "e_3003")
        self.assertEqual({c["pv"] for c in e3003["pch"]}, {"00"})

    def test_unknown_path(self):
        resp = FakeDaikin(FakeClock()).handle({"requests": [{"op": 2, "to": "/dsiot/edge/adr_0100.i_power"}]})
        self.assertEqual(resp["responses"], [{"fr": "/dsiot/edge/adr_0100.i_power", "rsc": 4000}])

    def test_sensors_follow_the_room(self):
        clock = FakeClock()
        dev = FakeDaikin(clock)
        write(dev, t.ACState(True, t.MODE_COOL, 260, t.FAN_AUTO))
        clock.advance_min(10)
        r = read(dev)
        self.assertEqual(r.temp, 270)
        self.assertTrue(r.hum < 72)

    def test_sensors_update_while_off(self):
        clock = FakeClock()
        dev = FakeDaikin(clock)
        dev.set_room(temp=24)
        clock.advance_min(60)
        self.assertEqual(read(dev).temp, 275)  # drifts towards outdoor 32 C

    def test_negative_outdoor(self):
        dev = FakeDaikin(FakeClock())
        dev.set_room(outdoor=-2.5)
        self.assertEqual(read(dev).outdoor_temp, -25)


class TestWrites(unittest.TestCase):
    def test_write_shows_in_next_read(self):
        dev = FakeDaikin(FakeClock())
        self.assertIsNone(write(dev, COOL))
        self.assertEqual(read(dev).ac, COOL)
        self.assertIsNone(write(dev, t.ACState(False, t.MODE_COOL, 245, "4")))
        self.assertEqual(read(dev).ac, t.ACState(False, t.MODE_COOL, 245, "4"))
        self.assertEqual(dev.writes, 2)

    def test_each_mode_keeps_its_fields(self):
        dev = FakeDaikin(FakeClock())
        write(dev, COOL)
        write(dev, t.ACState(True, t.MODE_FAN, None, "1"))
        self.assertEqual(read(dev).ac, t.ACState(True, t.MODE_FAN, None, "1"))
        write(dev, t.ACState(True, t.MODE_COOL))  # no target or fan: the cool fields are still there
        self.assertEqual(read(dev).ac, COOL)
        write(dev, t.ACState(True, t.MODE_DRY, None, "quiet"))
        write(dev, t.ACState(True, t.MODE_FAN))
        self.assertEqual(read(dev).ac, t.ACState(True, t.MODE_FAN, None, "1"))

    def test_streamer_write_rejected_and_nothing_applied(self):
        dev = FakeDaikin(FakeClock())
        self.assertEqual(codec.write_result(dev.handle(streamer_write())), "rsc 4000")
        self.assertEqual(read(dev).ac, OFF_FAN)

    def test_invalid_params_rejected(self):
        dev = FakeDaikin(FakeClock())
        cases = (
            ("e_A002", "p_01", "02"),
            ("e_3001", "p_01", "0700"),
            ("e_3001", "p_02", "42"),  # 33 C
            ("e_3001", "p_02", "13"),  # 9.5 C
            ("e_3001", "p_02", "3400"),
            ("e_3001", "p_09", "0900"),
            ("e_3001", "p_05", "00"),
            ("e_3003", "p_20", "00"),
            ("e_A00B", "p_01", "10"),
        )
        for group, param, pv in cases:
            body = codec.write_request(t.ACState(True, t.MODE_COOL))
            body["requests"][0]["pc"]["pch"][0]["pch"].append({"pn": group, "pch": [{"pn": param, "pv": pv}]})
            self.assertEqual(codec.write_result(dev.handle(body)), "rsc 4000", (group, param, pv))
        self.assertEqual(read(dev).ac, OFF_FAN)

    def test_heat_and_auto_writes_accepted_like_the_unit(self):
        # The codec never sends them, but the unit takes them; the remote sets them too.
        body = codec.write_request(t.ACState(True, t.MODE_COOL))
        body["requests"][0]["pc"]["pch"][0]["pch"][1]["pch"] = [{"pn": "p_01", "pv": "0100"}]
        dev = FakeDaikin(FakeClock())
        self.assertIsNone(codec.write_result(dev.handle(body)))
        self.assertEqual(read(dev).ac, t.ACState(True, t.MODE_HEAT, 220, t.FAN_AUTO))

    def test_mixed_or_malformed_bodies(self):
        dev = FakeDaikin(FakeClock())
        mixed = {"requests": codec.READ_REQUEST["requests"] + codec.write_request(COOL)["requests"]}
        for body in (mixed, {}, None, [], {"requests": []}, {"requests": "x"}, {"requests": [{"op": 5}]}):
            resp = dev.handle(body)
            self.assertTrue(resp["responses"])
            self.assertEqual({r["rsc"] for r in resp["responses"]}, {4000})
        self.assertEqual(read(dev).ac, OFF_FAN)


class TestScript(unittest.TestCase):
    def test_remote_press_visible_in_next_read(self):
        clock = FakeClock()
        dev = FakeDaikin(clock, script=[(5, "remote", t.ACState(True, t.MODE_HEAT, 230, "2"))])
        clock.advance_min(4)
        self.assertEqual(read(dev).ac, OFF_FAN)
        clock.advance_min(1)
        self.assertEqual(read(dev).ac, t.ACState(True, t.MODE_HEAT, 230, "2"))

    def test_remote_sets_only_that_modes_fields(self):
        dev = FakeDaikin(FakeClock())
        write(dev, COOL)
        dev.remote(t.ACState(True, t.MODE_AUTO, 250, "quiet"))
        self.assertEqual(read(dev).ac, t.ACState(True, t.MODE_AUTO, 250, "quiet"))
        write(dev, t.ACState(True, t.MODE_COOL))
        self.assertEqual(read(dev).ac, COOL)

    def test_remote_bypasses_rejection(self):
        dev = FakeDaikin(FakeClock())
        dev.reject_writes(5)
        dev.remote(COOL)
        self.assertEqual(read(dev).ac, COOL)

    def test_remote_unknown_mode(self):
        with self.assertRaises(ValueError):
            FakeDaikin(FakeClock()).remote(t.ACState(True, t.MODE_UNKNOWN))

    def test_room_steps_between_events(self):
        # Cool from minute 0 to 10, then off: the room only cooled for those 10 minutes.
        clock = FakeClock()
        script = [(0, "remote", t.ACState(True, t.MODE_COOL, 200, t.FAN_AUTO)), (10, "remote", OFF_FAN)]
        dev = FakeDaikin(clock, script=script)
        clock.advance_min(20)
        dev.sync()
        self.assertTrue(26.5 < dev.room.temp < 28.5, dev.room.temp)
        self.assertEqual(dev.ac(), OFF_FAN)

    def test_unreachable_then_back(self):
        clock = FakeClock()
        dev = FakeDaikin(clock, script=[(2, "unreachable", 3)])
        clock.advance_min(2)
        self.assertIsNone(dev.handle(codec.READ_REQUEST))
        self.assertIsNone(dev.handle(codec.write_request(COOL)))
        clock.advance_min(2)
        self.assertIsNone(dev.handle(codec.READ_REQUEST))
        clock.advance_min(1)
        self.assertEqual(read(dev).ac, OFF_FAN)  # the write while down never arrived
        self.assertEqual(dev.writes, 0)

    def test_reject_writes(self):
        dev = FakeDaikin(FakeClock(), script=[(0, "reject_writes", 2)])
        self.assertEqual(write(dev, COOL), "rsc 4000")
        self.assertEqual(write(dev, COOL), "rsc 4000")
        self.assertEqual(read(dev).ac, OFF_FAN)
        self.assertIsNone(write(dev, COOL))
        self.assertEqual(read(dev).ac, COOL)

    def test_ignore_writes(self):
        dev = FakeDaikin(FakeClock())
        dev.ignore_writes(1)
        self.assertIsNone(write(dev, COOL))
        self.assertEqual(read(dev).ac, OFF_FAN)
        self.assertIsNone(write(dev, COOL))
        self.assertEqual(read(dev).ac, COOL)

    def test_set_room(self):
        clock = FakeClock()
        dev = FakeDaikin(clock, script=[(30, "set_room", {"temp": 33, "hum": 60})])
        clock.advance_min(31)
        r = read(dev)
        self.assertEqual((r.temp, r.hum), (330, 60))

    def test_events_fire_in_order(self):
        clock = FakeClock()
        script = [(3, "remote", OFF_FAN), (1, "remote", COOL)]  # out of order in the list
        dev = FakeDaikin(clock, script=script)
        clock.advance_min(2)
        self.assertEqual(read(dev).ac, COOL)
        clock.advance_min(2)
        self.assertEqual(read(dev).ac, OFF_FAN)

    def test_unknown_event(self):
        clock = FakeClock()
        dev = FakeDaikin(clock, script=[(0, "explode", None)])
        with self.assertRaises(ValueError):
            dev.sync()

    def test_across_the_ticks_wrap(self):
        def scenario(start):
            clock = FakeClock(start)
            script = [(1, "remote", COOL), (3, "unreachable", 2), (6, "remote", OFF_FAN)]
            dev = FakeDaikin(clock, script=script)
            clock.advance_min(1)
            self.assertEqual(read(dev).ac, COOL)
            clock.advance_min(2)
            self.assertIsNone(dev.handle(codec.READ_REQUEST))
            clock.advance_min(2)
            self.assertEqual(read(dev).ac, COOL)
            clock.advance_min(1)
            self.assertEqual(read(dev).ac, OFF_FAN)
            return clock, dev

        clock, wrapped = scenario(time.ticks_add(0, -2 * 60 * 1000))
        self.assertTrue(clock() < wrapped.start)  # the raw ticks wrapped
        _, plain = scenario(0)
        self.assertAlmostEqual(wrapped.room.temp, plain.room.temp)  # the room didn't notice
        self.assertTrue(wrapped.room.temp < 28.5)
