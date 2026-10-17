# SPDX-License-Identifier: GPL-3.0-only
# The app (shell/app.py) against the simulator over HTTP, in virtual time (sim/harness.py's Scheduler).
import asyncio
import json
import unittest

from sim.clock import FakeClock
from sim.device import FakeDaikin
from sim.harness import Scheduler
from sim.server import DEFAULT_PORT, serve
from smartac.board.host import HostBoard
from smartac.core import config
from smartac.core import types as t
from smartac.shell.app import MAX_ACTIONS, SETTLE_MS, App
from smartac.shell.store import MAX_EVENT_LINES, MemoryStore

HOST = "127.0.0.1"
DRY = t.ACState(True, t.MODE_DRY, None, t.FAN_AUTO)


class Stop(Exception):
    pass


class _App(App):
    def __init__(self, world, board, observe=False):
        super().__init__(board, world.store, HOST, DEFAULT_PORT, observe=observe)
        self.world = world

    def _step(self, now, reading, actions, readback=False):
        cmds = super()._step(now, reading, actions, readback)
        self.world.steps.append((self.world.sched.t, readback, self.controller.state, cmds))
        return cmds


class World:
    # The default room (29 C, 80 %) makes the day profile start "humid" on the second read.
    def __init__(self, active="off"):
        self.clock = FakeClock()
        self.sched = Scheduler(self.clock)
        self.device = FakeDaikin(self.clock)
        self.store = MemoryStore()
        profiles = config.default_profiles()
        profiles["active"] = active
        self.store.save("profiles", profiles)
        self.steps = []

    def app(self, observe=False):
        return _App(self, HostBoard(clock=self.clock, sleep=self.sched.sleep), observe)

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
                self.sched.entries = [e for e in self.sched.entries if e[2] is not stop]  # if the run ended early
                server.close()
                await server.wait_closed()

        asyncio.run(main())

    def polls(self):
        return [s[0] for s in self.steps if not s[1]]

    def kinds(self):
        return [p[0] for s in self.steps for k, p in s[3] if k == t.CMD_EVENT]


def stop_after_write(app, check=None):
    # Cut the power right after the first write reached the AC.
    write = app.client.write

    async def cut(state):
        if check is not None:
            check(state)
        await write(state)
        raise Stop()

    app.client.write = cut


class TestTick(unittest.TestCase):
    def test_cycle_starts_and_the_read_back_confirms(self):
        w = World("day")
        app = w.app()
        w.run(app, 70 * 1000)
        self.assertEqual([(s[0], s[1]) for s in w.steps], [(0, False), (60000, False), (60000 + SETTLE_MS, True)])
        self.assertEqual(w.steps[1][3][1], (t.CMD_WRITE, DRY))
        self.assertEqual(w.device.ac(), DRY)
        ctl = app.controller
        self.assertEqual((ctl.state, ctl.expected, ctl.written), (t.RUNNING, DRY, None))
        s = app.status()
        self.assertEqual((s["reads"], s["writes"], s["write_errors"], s["device"]["uptime_s"]), (3, 1, 0, 70))
        self.assertEqual(s["ac"], {"reachable": True, "power": True, "mode": "dry", "target_c": None, "fan": "auto"})
        self.assertEqual(s["state"], t.RUNNING)

    def test_rejected_write_is_counted_and_judged_by_the_read_back(self):
        w = World("day")
        w.device.reject_writes(1)
        app = w.app()
        w.run(app, 70 * 1000)
        self.assertEqual((app.writes, app.write_errors, app.last_write_error), (1, 1, "rsc 4000"))
        self.assertEqual((app.controller.fails, app.controller.written), (1, DRY))

    def test_action_wakes_the_control_task(self):
        w = World()
        app = w.app()
        w.sched.at(10 * 1000, lambda: app.action({"action": "pause"}))
        w.run(app, 75 * 1000)
        self.assertEqual(w.polls(), [0, 10000, 70000])
        self.assertEqual(app.controller.state, t.PAUSED)

    def test_queue_is_bounded(self):
        app = World().app()
        queued = [app.action({"action": "stop"}) for _ in range(MAX_ACTIONS + 1)]
        self.assertEqual(queued, [True] * MAX_ACTIONS + [False])
        self.assertTrue(app.wake.is_set())


class TestStore(unittest.TestCase):
    def test_state_is_saved_before_the_write_is_sent(self):
        w = World("day")
        app = w.app()
        seen = []
        stop_after_write(app, lambda state: seen.append(w.store.load("state")))
        w.run(app, 70 * 1000)
        self.assertEqual(seen[0]["state"], t.RUNNING)
        self.assertEqual(seen[0]["written"], [True, "dry", None, "auto"])

    def test_reboot_after_the_write_finishes_the_cycle(self):
        w = World("day")
        app = w.app()
        stop_after_write(app)
        w.run(app, 70 * 1000)
        self.assertEqual(w.device.ac(), DRY)  # the write landed, the read-back never came
        app = w.app()
        self.assertEqual(app.boot, 2)
        w.run(app, 30 * 60 * 1000)
        # Resolved from the snapshot: the dry phase isn't resumed, its fan phase runs, then off.
        self.assertEqual(w.kinds(), [t.EV_CYCLE_START, t.EV_PHASE, t.EV_CYCLE_END])
        self.assertEqual(app.controller.state, t.COOLDOWN)
        self.assertFalse(w.device.ac().power)
        lines = [json.loads(line) for line in w.store.events]
        self.assertEqual([(e["boot"], e["kind"]) for e in lines], [(1, "boot"), (2, "boot"), (2, "cycle_end")])

    def test_profile_switch_updates_the_stored_active(self):
        w = World()
        app = w.app()
        w.sched.at(5 * 1000, lambda: app.action({"action": "set_profile", "profile": "night"}))
        w.run(app, 10 * 1000)
        self.assertEqual(w.store.load("profiles")["active"], "night")
        self.assertEqual(w.store.load("state")["profile"], "night")
        self.assertEqual(w.app().controller.active, "night")

    def test_broken_documents_fall_back_to_defaults(self):
        w = World()
        w.store.save("profiles", {"version": 1})
        w.store.docs["settings"] = "{not json"
        app = w.app()
        self.assertEqual(app.controller.profiles, config.default_profiles())
        self.assertEqual(app.controller.settings, config.default_settings(HOST))
        self.assertEqual([x["path"] for x in app.warnings], ["profiles"])
        w.store.save("profiles", config.default_profiles())
        w.store.save("settings", dict(config.default_settings(HOST), poll_s=5))
        self.assertEqual([x["path"] for x in w.app().warnings], ["settings"])

    def test_memory_store(self):
        store = MemoryStore()
        self.assertIsNone(store.load("state"))
        self.assertEqual([store.next_boot(), store.next_boot()], [1, 2])
        store.docs["boot"] = "garbage"
        self.assertEqual(store.next_boot(), 1)
        store.save("state", {"a": [1, None]})
        self.assertEqual(store.docs["state"], '{"a": [1, null]}')
        for i in range(MAX_EVENT_LINES + 100):
            store.append_event(str(i))
        self.assertEqual((len(store.events), store.events[-1]), (MAX_EVENT_LINES, str(MAX_EVENT_LINES + 99)))

    def test_boot_event_reaches_the_log(self):
        w = World()
        w.app()
        self.assertEqual(json.loads(w.store.events[0])["kind"], t.EV_BOOT)


class TestObserve(unittest.TestCase):
    def test_reads_and_records_but_never_writes(self):
        # The day profile would start a dry cycle on the second read (TestTick); observe mode sends nothing.
        w = World("day")
        app = w.app(observe=True)
        self.assertIsNone(app.controller)
        w.run(app, 3 * 60 * 1000 + 1000)
        self.assertEqual((w.device.writes, app.writes, w.steps), (0, 0, []))
        self.assertFalse(w.device.ac().power)
        self.assertEqual(app.reads, 4)
        d = json.loads("".join(app.history_chunks(1440)))
        self.assertEqual(len(d["temp"]), 1440)
        self.assertEqual(d["temp"][-4:], [29.0] * 4)
        self.assertEqual(d["power"][-4:], [False] * 4)
        self.assertEqual(d["last_age_s"], 1)

    def test_status_shape(self):
        w = World()
        app = w.app(observe=True)
        w.run(app, 1000)
        s = app.status()
        self.assertEqual((s["state"], s["profile"], s["dry_run"]), (t.OBSERVE, None, False))
        self.assertEqual(s["room"], {"temp": 29.0, "hum": 80})
        self.assertEqual(s["ac"]["reachable"], True)
        self.assertEqual((s["last_error"], s["unreachable_s"], s["config_warning"]), (None, None, None))
        d = s["device"]
        self.assertEqual((d["boot"], d["reset_cause"], d["last_crash"]), (1, "power_on", None))
        self.assertEqual(d["resets"], {"power_on": 1})
        self.assertEqual((d["uptime_s"], d["rssi"]), (1, -50))
        app.stats["wifi_reconnects"] = 2
        self.assertEqual(app.status()["device"]["wifi_reconnects"], 2)
        json.dumps(s)

    def test_unreachable_time_and_seen_ac(self):
        w = World()
        app = w.app(observe=True)
        w.run(app, 1000)
        self.assertTrue(app.seen_ac)
        w.device.unreachable(10)
        w.run(app, 5 * 60 * 1000)
        s = app.status()
        self.assertFalse(s["ac"]["reachable"])
        self.assertEqual(s["room"], {"temp": None, "hum": None})
        self.assertTrue(s["last_error"])
        # The second run starts a new control loop: the first failed read is at 0:01, now is 5:00.
        self.assertEqual(s["unreachable_s"], 299)
        w.run(app, 12 * 60 * 1000)
        self.assertIsNone(app.status()["unreachable_s"])

    def test_never_seen_without_an_address(self):
        w = World()
        app = _App(w, HostBoard(clock=w.clock, sleep=w.sched.sleep), True)
        app.client = None
        w.run(app, 1000)
        self.assertFalse(app.seen_ac)
        self.assertEqual(app.status()["last_error"], "no AC address")
        self.assertEqual(app.status()["unreachable_s"], 1)

    def test_crash_and_reset_cause_from_the_store(self):
        w = World()
        w.store.save_crash("Traceback (most recent call last):\n  File x\nMemoryError: memory allocation failed\n")
        w.store.reset_counts("watchdog")
        app = _App(w, HostBoard(clock=w.clock, sleep=w.sched.sleep, cause="crash"), True)
        d = app.status()["device"]
        self.assertEqual(d["last_crash"], "MemoryError: memory allocation failed")
        self.assertEqual((d["reset_cause"], d["resets"]), ("crash", {"watchdog": 1, "crash": 1}))
        self.assertEqual(json.loads(w.store.events[0])["msg"], "boot 1 (crash)")
