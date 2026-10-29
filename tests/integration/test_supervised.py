# SPDX-License-Identifier: GPL-3.0-only
# The M4 runner (sim/supervised.py) against the simulator in virtual time: the approval gate, the fault toggle, the
# scaled minute and the runner's routes. Approval is a callable here, never stdin.
import asyncio
import json
import unittest

import simhttp

from sim.clock import FakeClock
from sim.device import FakeDaikin
from sim.harness import Scheduler
from sim.server import DEFAULT_PORT, serve
from sim.supervised import (
    GAP_S,
    TOKEN,
    Gate,
    SupervisedApp,
    add_routes,
    scale_minute,
    session_profiles,
    session_settings,
)
from smartac.board.host import HostBoard
from smartac.core import config, controller
from smartac.core import types as t
from smartac.shell import api
from smartac.shell.store import MemoryStore

HOST = "127.0.0.1"
PORT = DEFAULT_PORT + 3
MINUTE_S = 15
FORM = "application/x-www-form-urlencoded"
COOL = t.ACState(True, t.MODE_COOL, 260, t.FAN_AUTO)
FAN = t.ACState(True, t.MODE_FAN, None, "3")


class Stop(Exception):
    pass


class World:
    # The session's own profiles and settings; the default room (29 C) meets the trigger on the first read.
    def __init__(self, *answers):
        self.clock = FakeClock()
        self.sched = Scheduler(self.clock)
        self.device = FakeDaikin(self.clock)
        self.store = MemoryStore()
        self.store.save("profiles", session_profiles())
        self.store.save("settings", session_settings())
        self.answers = list(answers)
        self.asked = []
        self.asked_ms = []
        self.notes = []
        self.on_ask = None

    def approve(self, question):
        self.asked.append(question)
        self.asked_ms.append(self.clock())
        if self.on_ask is not None:
            self.on_ask()
        return self.answers.pop(0)

    def app(self):
        board = HostBoard(clock=self.clock, sleep=self.sched.sleep)
        return SupervisedApp(board, self.store, HOST, DEFAULT_PORT, self.approve, self.notes.append)

    def run(self, app, until_ms):
        def stop():
            raise Stop()

        async def main():
            server = await serve(self.device)
            try:
                self.sched.at(until_ms, stop)
                await app.control()
            except Stop:
                pass
            finally:
                server.close()
                await server.wait_closed()

        asyncio.run(main())

    def kinds(self):
        return [json.loads(line)["kind"] for line in self.store.events]


class Scaled(unittest.TestCase):
    def setUp(self):
        self.old_min_ms = scale_minute(MINUTE_S)

    def tearDown(self):
        controller.MIN_MS = self.old_min_ms


class TestGate(Scaled):
    def test_approved_writes_run_a_full_cycle(self):
        # cool 16 min (4 real) -> fan 16 min -> off -> cooldown 16 min -> idle, each write read back.
        w = World(True, True, True)
        app = w.app()
        w.run(app, 800 * 1000)
        self.assertEqual(w.kinds(), [t.EV_BOOT, t.EV_CYCLE_START, t.EV_PHASE, t.EV_CYCLE_END])
        self.assertEqual(app.controller.state, t.IDLE)
        self.assertFalse(w.device.ac().power)
        self.assertEqual((app.writes, app.write_errors, w.device.writes), (3, 0, 3))
        self.assertEqual(app.decisions, {"approved": 3, "declined": 0, "not sent": 0})
        self.assertTrue("  write: on cool 26.0, fan auto\n" in w.asked[0])
        self.assertTrue(w.asked[1].endswith("  write: on fan, fan 3\napprove? [y/N] "))
        self.assertTrue("write: off" in w.asked[2])
        self.assertEqual(w.notes[0], "write 1 approved: on cool 26.0, fan auto (accepted)")
        gaps = [b - a for a, b in zip(w.asked_ms, w.asked_ms[1:])]
        self.assertTrue(all(g >= GAP_S * 1000 for g in gaps), gaps)
        self.assertFalse("last write" in w.asked[0])
        self.assertTrue(", last write 4:" in w.asked[1] and "under" not in w.asked[1])

    def test_two_declines_are_a_failed_write_not_a_human(self):
        w = World(False, False)
        app = w.app()
        w.run(app, 70 * 1000)
        kinds = w.kinds()
        self.assertTrue(t.EV_WRITE_FAILED in kinds)
        self.assertFalse(t.EV_MANUAL in kinds)
        self.assertEqual(app.controller.state, t.MANUAL)
        self.assertEqual((app.writes, app.write_errors, app.last_write_error), (2, 2, "declined"))
        self.assertEqual((w.device.writes, app.decisions["declined"]), (0, 2))
        self.assertFalse(w.device.ac().power)

    def test_a_change_while_waiting_is_not_written_over(self):
        # The remote turns the AC on while the prompt is open: no write, and the next poll sees a human.
        w = World(True)
        w.on_ask = lambda: w.device.remote(t.ACState(True, t.MODE_COOL, 240, t.FAN_AUTO))
        app = w.app()
        w.run(app, 70 * 1000)
        self.assertEqual(w.device.writes, 0)
        self.assertEqual(app.decisions, {"approved": 0, "declined": 0, "not sent": 1})
        self.assertEqual(app.last_write_error, "changed while awaiting approval")
        self.assertTrue(w.notes[0].startswith("write 1 not sent, AC on cool 24.0, fan auto since the tick"))
        self.assertEqual(w.kinds()[-1], t.EV_MANUAL)
        self.assertEqual(app.controller.state, t.MANUAL)

    def test_every_new_client_is_gated(self):
        app = World().app()
        first = app.client
        self.assertIsInstance(first, Gate)
        app._set_client(session_settings())
        self.assertIs(app.client, first)
        app._set_client(dict(session_settings(), sensor_limits={"temp": [5, 45], "hum": [5, 100]}))
        self.assertIsInstance(app.client, Gate)
        self.assertIsNot(app.client.client, first.client)


class TestFault(Scaled):
    def test_reads_fail_until_cleared_then_the_cycle_starts(self):
        w = World(True)
        app = w.app()
        app.fault = True

        def clear():
            app.fault = False

        w.sched.at(150 * 1000, clear)
        w.run(app, 190 * 1000)
        self.assertEqual(w.kinds(), [t.EV_BOOT, t.EV_UNREACHABLE, t.EV_CYCLE_START])
        self.assertEqual((app.read_errors, len(w.asked)), (3, 1))
        self.assertEqual(w.device.ac(), COOL)


class TestScale(unittest.TestCase):
    def test_scale_minute_returns_the_old_value(self):
        old = scale_minute(MINUTE_S)
        try:
            self.assertEqual((old, controller.MIN_MS), (60 * 1000, MINUTE_S * 1000))
        finally:
            controller.MIN_MS = old
        self.assertEqual(controller.MIN_MS, 60 * 1000)

    def test_session_config_is_valid(self):
        profiles, settings = session_profiles(), session_settings()
        self.assertEqual(config.validate_profiles(profiles, settings["hard_cap_min"]), ([], []))
        self.assertEqual(config.validate_settings(settings, profiles), ([], []))
        self.assertIsNone(settings["ac_ip"])
        for rule in profiles["profiles"]["m4"]["rules"]:
            self.assertTrue(all(p["max_min"] * MINUTE_S >= GAP_S for p in rule["phases"]))
            self.assertTrue(rule["cooldown_min"] * MINUTE_S >= GAP_S)
        self.assertTrue(settings["manual_hold_min"] * MINUTE_S >= GAP_S)
        self.assertTrue(settings["finish_fan_min"] * MINUTE_S >= GAP_S)

    def test_session_config_keeps_the_gap_at_any_minute(self):
        for minute_s in (5, 7.5, 15, 20, 60):
            profiles, settings = session_profiles(minute_s), session_settings(minute_s)
            self.assertEqual(config.validate_profiles(profiles, settings["hard_cap_min"]), ([], []), minute_s)
            self.assertEqual(config.validate_settings(settings, profiles), ([], []), minute_s)
            for rule in profiles["profiles"]["m4"]["rules"]:
                self.assertTrue(all(p["max_min"] * minute_s >= GAP_S for p in rule["phases"]), minute_s)


def post(path, body, token=TOKEN, kind="application/json"):
    data = json.dumps(body).encode()
    head = "POST %s HTTP/1.1\r\nHost: mac\r\nContent-Type: %s\r\nContent-Length: %d\r\n" % (
        path, kind, len(data))
    if token is not None:
        head += "X-Token: %s\r\n" % token
    return (head + "\r\n").encode() + data


class TestRoutes(unittest.TestCase):
    def exchange(self, *requests, device=None):
        notes = []

        async def main():
            app = SupervisedApp(HostBoard(clock=FakeClock()), MemoryStore(), HOST, DEFAULT_PORT, None, notes.append)
            web = api.make(app, "/nowhere", token=TOKEN)
            add_routes(web, app, notes.append, device)
            task = asyncio.create_task(web.start_server(HOST, PORT))
            await asyncio.sleep_ms(50)
            try:
                replies = []
                for r in requests:
                    replies.append(await simhttp.raw(PORT, r))
                return app, replies
            finally:
                web.shutdown()
                await task

        app, replies = asyncio.run(main())
        return app, [r[0] for r in replies], notes

    def test_fault_toggle(self):
        app, codes, notes = self.exchange(post("/run/fault", {"read": "unreachable"}))
        self.assertEqual((codes, app.fault, notes), ([200], True, ["fault: reads unreachable"]))
        app, codes, _ = self.exchange(post("/run/fault", {"read": "unreachable"}), post("/run/fault", {"read": "ok"}))
        self.assertEqual((codes, app.fault), ([200, 200], False))

    def test_curl_form_type_is_read_as_json(self):
        # curl -d sends application/x-www-form-urlencoded; the body is JSON all the same.
        app, codes, _ = self.exchange(post("/run/fault", {"read": "unreachable"}, kind=FORM))
        self.assertEqual((codes, app.fault), ([200], True))

    def test_api_action_is_queued_and_logged(self):
        run = {"action": "run", "profile": "day_morning", "rule": 1}
        app, codes, notes = self.exchange(post("/api/action", run))
        self.assertEqual((codes, app.queue, notes), ([202], [run], ["action %s" % app.queue[0]]))

    def test_bad_requests(self):
        app, codes, notes = self.exchange(
            post("/api/action", {"action": "stop"}, token=None),
            post("/api/action", {"profile": "m4"}),
            post("/run/fault", {"read": "maybe"}),
            post("/run/fault", {"read": "unreachable"}, token="wrong"),
        )
        self.assertEqual((codes, app.queue, app.fault, notes), ([401, 400, 400, 401], [], False, []))

    def test_sim_remote_and_outage(self):
        device = FakeDaikin(FakeClock())
        remote = {"remote": {"power": True, "mode": "cool", "target_c": 24.5, "fan": "quiet"}}
        _, codes, notes = self.exchange(post("/run/sim", remote), post("/run/sim", {"unreachable": 2}), device=device)
        self.assertEqual(codes, [200, 200])
        self.assertEqual(device.ac(), t.ACState(True, t.MODE_COOL, 245, t.FAN_QUIET))
        self.assertFalse(device.reachable())
        self.assertEqual(notes, ["sim: remote on cool 24.5, fan quiet", "sim: AC silent for 2 min"])

    def test_sim_route_needs_the_simulator_and_a_valid_body(self):
        _, codes, _ = self.exchange(post("/run/sim", {"unreachable": 1}))
        self.assertEqual(codes, [409])
        device = FakeDaikin(FakeClock())
        bad = ({"remote": {"mode": "cool"}}, {"unreachable": 0}, {})
        _, codes, _ = self.exchange(*[post("/run/sim", doc) for doc in bad], device=device)
        self.assertEqual(codes, [400, 400, 400])
        self.assertFalse(device.ac().power)
