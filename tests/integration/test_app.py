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
from smartac.core.events import to_flash
from smartac.shell.app import MAX_ACTIONS, SETTLE_MS, App
from smartac.shell.store import MAX_EVENT_LINES, MemoryStore

HOST = "127.0.0.1"
COOL = t.ACState(True, t.MODE_COOL, 260, "3")  # the day profile's first write here
FAN3 = t.ACState(True, t.MODE_FAN, None, "3")
MIN = 60 * 1000


class Stop(Exception):
    pass


class _App(App):
    def __init__(self, world, board, observe=False, shadow=False):
        super().__init__(board, world.store, HOST, DEFAULT_PORT, observe=observe, shadow=shadow)
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

    def app(self, observe=False, shadow=False):
        return _App(self, HostBoard(clock=self.clock, sleep=self.sched.sleep), observe, shadow)

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
        self.assertEqual(w.steps[1][3][1], (t.CMD_WRITE, COOL))
        self.assertEqual(w.device.ac(), COOL)
        ctl = app.controller
        self.assertEqual((ctl.state, ctl.expected, ctl.written), (t.RUNNING, COOL, None))
        s = app.status()
        self.assertEqual((s["reads"], s["writes"], s["write_errors"], s["device"]["uptime_s"]), (3, 1, 0, 70))
        self.assertEqual(s["ac"], {"reachable": True, "power": True, "mode": "cool", "target_c": 26.0, "fan": "3"})
        self.assertEqual(s["state"], t.RUNNING)

    def test_rejected_write_is_counted_and_judged_by_the_read_back(self):
        w = World("day")
        w.device.reject_writes(1)
        app = w.app()
        w.run(app, 70 * 1000)
        self.assertEqual((app.writes, app.write_errors, app.last_write_error), (1, 1, "rsc 4000"))
        self.assertEqual((app.controller.fails, app.controller.written), (1, COOL))

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
        self.assertEqual(seen[0]["written"], [True, "cool", 260, "3"])

    def test_reboot_after_the_write_finishes_the_cycle(self):
        w = World("day")
        app = w.app()
        stop_after_write(app)
        w.run(app, 70 * 1000)
        self.assertEqual(w.device.ac(), COOL)  # the write landed, the read-back never came
        app = w.app()
        self.assertEqual(app.boot, 2)
        w.run(app, 30 * 60 * 1000)
        # Resolved from the snapshot: the dry phase isn't resumed, its fan phase runs, then off.
        self.assertEqual(w.kinds(), [t.EV_CYCLE_START, t.EV_PHASE, t.EV_CYCLE_END])
        self.assertEqual(app.controller.state, t.COOLDOWN)
        self.assertFalse(w.device.ac().power)
        lines = [json.loads(line) for line in w.store.events]
        self.assertEqual([(e["boot"], e["kind"]) for e in lines],
                         [(1, "boot"), (1, "cycle_start"), (2, "boot"), (2, "phase"), (2, "cycle_end")])

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

    def test_full_flash_counts_instead_of_resetting(self):
        # The boot counters and the log can't be written; the profiles and the snapshot still can.
        w = World("day")
        w.store.reset_counts("power_on")
        save = w.store.save

        def full(name, doc):
            if name in ("boot", "resets"):
                raise OSError(28)
            save(name, doc)

        w.store.save = full
        w.store.fail_append = True
        app = w.app()
        self.assertEqual((app.boot, app.resets), (1, {"power_on": 2}))
        self.assertEqual(app.status()["config_warning"], "boot: flash write failed, counters not saved")
        self.assertEqual(app.log_errors, 1)  # the boot event
        w.run(app, 70 * 1000)
        self.assertEqual(app.controller.state, t.RUNNING)
        app.log(t.EV_HEAP, "free 1 block 1")
        kinds = [r["kind"] for r in app.event_rows(100)]
        self.assertEqual(kinds, [t.EV_CYCLE_START, t.EV_BOOT])  # the RAM ring has them all
        refused = len([k for k in kinds if to_flash(k, app.events.flash_kinds)]) + 1  # and the heap line
        self.assertEqual((w.store.events, app.status()["device"]["log_errors"]), ([], refused))
        self.assertEqual(w.app().boot, 1)  # still unsaved: the next boot counts from the same value


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
        self.assertEqual((s["state"], s["profile"], s["dry_run"], s["poll_s"]), (t.OBSERVE, None, False, 60))
        self.assertEqual((s["shadow"], s["profiles"]), (False, []))
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


def flash(store):
    return [json.loads(line) for line in store.events]


class TestShadow(unittest.TestCase):
    def world(self, active="day"):
        # settings.json asks for real writes: shadow mode must ignore that.
        w = World(active)
        w.store.save("settings", dict(config.default_settings(HOST), dry_run=False))
        return w

    def test_whole_cycles_without_a_write(self):
        w = self.world()
        app = w.app(shadow=True)
        w.run(app, 6 * 60 * 60 * 1000)
        kinds = w.kinds()
        for kind in (t.EV_CYCLE_START, t.EV_WOULD, t.EV_PHASE, t.EV_CYCLE_END):
            self.assertIn(kind, kinds)
        self.assertEqual((w.device.writes, app.writes, app.write_errors), (0, 0, 0))
        self.assertFalse(w.device.ac().power)
        self.assertFalse(any(s[1] for s in w.steps))  # no read-back: nothing was sent
        self.assertFalse(t.EV_BLOCKED in kinds)
        # The decisions reach the flash log, each with the reading it was made on.
        lines = flash(w.store)
        would = [d for d in lines if d["kind"] == t.EV_WOULD]
        self.assertEqual(len(would), kinds.count(t.EV_WOULD))
        self.assertEqual(would[0]["msg"], "would set cool 26.0, fan 3")
        self.assertEqual((would[0]["temp"], would[0]["hum"]), (29.0, 80))
        self.assertTrue(any(d["kind"] == t.EV_CYCLE_START for d in lines))
        self.assertEqual(lines[0]["kind"], t.EV_BOOT)

    def test_status(self):
        w = self.world()
        app = w.app(shadow=True)
        w.run(app, 1000)
        s = app.status()
        self.assertEqual((s["shadow"], s["dry_run"], s["profile"], s["state"]), (True, True, "day", t.IDLE))
        self.assertEqual([p["name"] for p in s["profiles"]], ["away", "day", "night"])
        self.assertEqual(s["profiles"][1], {"name": "day", "label": "Day", "rules": ["too_hot", "humid"]})
        self.assertFalse(app.settings["dry_run"])  # settings.json itself is left as it is
        json.dumps(s)

    def test_restored_live_cycle_is_not_written(self):
        # A real cycle (M4) was cut right after its first write; the next boot is in shadow mode.
        w = self.world()
        app = w.app()
        stop_after_write(app)
        w.run(app, 70 * 1000)
        self.assertEqual((w.device.writes, w.store.load("state")["cycle"]["dry_run"]), (1, False))
        w.device.remote(t.ACState(False, t.MODE_COOL, 260, "3"))  # the owner switched it off
        app = w.app(shadow=True)
        self.assertTrue(app.controller.cycle["dry_run"])
        w.steps = []
        w.run(app, 3 * 60 * 60 * 1000)
        self.assertEqual((w.device.writes, app.writes), (1, 0))
        self.assertIn(t.EV_WOULD, w.kinds())
        self.assertFalse(t.EV_BLOCKED in w.kinds())

    def test_backstop_blocks_a_write(self):
        # A write that gets past dry_run anyway (a bug) is logged, not sent, and not counted.
        w = self.world()
        app = w.app(shadow=True)
        step = app._step

        def leaky(now, reading, actions, readback=False):
            return step(now, reading, actions, readback) + [(t.CMD_WRITE, COOL)]

        app._step = leaky
        w.run(app, 61 * 1000)
        self.assertEqual((w.device.writes, app.writes), (0, 0))
        self.assertFalse(w.device.ac().power)
        self.assertEqual(w.polls(), [0, 60000])
        blocked = [d for d in flash(w.store) if d["kind"] == t.EV_BLOCKED]
        self.assertEqual(len(blocked), 2)
        self.assertTrue(blocked[0]["msg"].startswith("not sent: ACState(True"), blocked[0]["msg"])
        self.assertEqual(blocked[0]["temp"], 29.0)

    def test_profile_action_starts_from_off(self):
        w = self.world("off")
        app = w.app(shadow=True)
        w.sched.at(5 * 1000, lambda: app.action({"action": "set_profile", "profile": "day", "cycle": "finish"}))
        w.run(app, 70 * 1000)
        self.assertEqual(w.store.load("profiles")["active"], "day")
        self.assertEqual([(d["kind"], d["msg"]) for d in flash(w.store)][1], (t.EV_PROFILE, "profile day"))
        self.assertEqual(app.controller.state, t.RUNNING)
        self.assertEqual(w.device.writes, 0)


def day_short(doc):
    # Every day rule: 10 min of cool or dry and 5 min of fan (the room warms past too_hot's 29 C after a while).
    for r in doc["profiles"]["day"]["rules"]:
        r["phases"][0]["max_min"] = 10
        r["phases"][1]["max_min"] = 5
    return doc


def event_times(w, kind):
    return [s[0] for s in w.steps for k, p in s[3] if k == t.CMD_EVENT and p[0] == kind]


class TestActions(unittest.TestCase):
    # The Status tab's buttons (POST /api/action) through real ticks. World("day") starts "humid" (dry 60 min, then
    # fan 3 for 20, cooldown 45) at 1:00.
    def run_with(self, w, app, actions, until_ms):
        for at_ms, action in actions:
            w.sched.at(at_ms, lambda a=action: app.action(a))
        w.run(app, until_ms)

    def test_pause_finish_mid_phase(self):
        w = World("day")
        app = w.app()
        self.run_with(w, app, [(5 * MIN, {"action": "pause", "minutes": 60, "cycle": "finish"})], 6 * MIN)
        self.assertEqual((app.controller.state, w.device.ac()), (t.RUNNING, FAN3))  # the rule's fan phase
        self.assertTrue(app.status()["pause_pending"])
        w.run(app, 27 * MIN)
        self.assertEqual(app.controller.state, t.PAUSED)
        self.assertFalse(w.device.ac().power)
        self.assertEqual(w.kinds()[-3:], [t.EV_PAUSE, t.EV_PHASE, t.EV_CYCLE_END])

    def test_pause_leave_then_resume(self):
        w = World("day")
        app = w.app()
        self.run_with(w, app, [(5 * MIN, {"action": "pause", "minutes": None, "cycle": "leave"})], 10 * MIN)
        self.assertEqual((app.controller.state, w.device.ac()), (t.PAUSED, COOL))  # left running
        self.assertEqual(app.writes, 1)
        w.device.remote(t.ACState(False, t.MODE_COOL, 260, "3"))
        self.run_with(w, app, [(11 * MIN, {"action": "resume"})], 14 * MIN)
        self.assertEqual((app.controller.state, w.kinds()[-2:]), (t.IDLE, [t.EV_PAUSE, t.EV_RESUME]))

    def test_stop_runs_the_fan_phase_then_off(self):
        w = World("day")
        app = w.app()
        self.run_with(w, app, [(5 * MIN, {"action": "stop"})], 6 * MIN)
        self.assertEqual(w.device.ac(), FAN3)
        w.run(app, 27 * MIN)
        self.assertEqual(app.controller.state, t.COOLDOWN)
        self.assertFalse(w.device.ac().power)
        self.assertEqual((app.writes, app.write_errors), (3, 0))

    def test_stop_now_then_run_from_cooldown(self):
        w = World("day")
        app = w.app()
        self.run_with(w, app, [(5 * MIN, {"action": "stop_now"})], 7 * MIN)
        self.assertEqual(app.controller.state, t.COOLDOWN)
        self.assertEqual(w.device.ac().power, False)
        self.assertEqual(app.writes, 2)  # dry, then off: no fan phase
        self.assertFalse(t.EV_PHASE in w.kinds())
        self.run_with(w, app, [(10 * MIN, {"action": "run", "profile": "day", "rule": 0})], 11 * MIN)
        self.assertEqual(app.controller.state, t.RUNNING)
        self.assertEqual(app.status()["cycle"]["rule"], "too_hot")
        self.assertEqual(w.device.ac().mode, t.MODE_COOL)

    def test_a_refused_action_is_an_ignored_event(self):
        w = World("day")
        app = w.app()
        self.run_with(w, app, [(5 * MIN, {"action": "run", "profile": "day", "rule": 0})], 6 * MIN)
        self.assertEqual(w.kinds()[-1], t.EV_IGNORED)
        self.assertEqual(app.event_rows(1)[0]["msg"], "run ignored in RUNNING")

    def test_clear_manual_takes_over(self):
        w = World("day")
        w.device.remote(t.ACState(True, t.MODE_COOL, 240, "2"))
        app = w.app()
        w.run(app, 2 * MIN)
        self.assertEqual(app.controller.state, t.MANUAL)
        self.run_with(w, app, [(3 * MIN, {"action": "clear_manual"})], 4 * MIN)
        self.assertEqual(app.controller.state, t.TAKEOVER)
        self.assertEqual(w.device.ac(), t.ACState(True, t.MODE_FAN, None, "2"))

    def test_switch_with_leave_hands_the_run_over(self):
        w = World("day")
        app = w.app()
        switch = {"action": "set_profile", "profile": "night", "cycle": "leave"}
        self.run_with(w, app, [(5 * MIN, switch)], 6 * MIN)
        self.assertEqual((app.controller.state, app.controller.active, w.device.ac()), (t.MANUAL, "night", COOL))

    def test_ac_test_during_a_write(self):
        # The test shares the client's lock: it waits for the write's exchange instead of racing it.
        w = World("day")
        app = w.app()
        write = app.client.write
        replies = []

        async def test():
            replies.append(await app.test_ac())

        async def racing(state):
            asyncio.create_task(test())
            await asyncio.sleep_ms(0)
            return await write(state)

        app.client.write = racing
        w.run(app, 70 * 1000)
        self.assertEqual((app.writes, app.write_errors, w.device.ac()), (1, 0, COOL))
        self.assertEqual((replies[0]["reachable"], replies[0]["fw"]), (True, "3.6.0"))

    def test_ac_test_addresses(self):
        w = World()
        w.store.save("settings", dict(config.default_settings(), ac_ip="192.0.2.1"))
        other = w.app()  # tests 127.0.0.1 with a throwaway client
        closed = App(HostBoard(clock=w.clock), MemoryStore(), HOST, DEFAULT_PORT + 7, shadow=True)

        async def main():
            server = await serve(w.device)
            try:
                return await other.test_ac(HOST), await closed.test_ac()
            finally:
                server.close()
                await server.wait_closed()

        good, refused = asyncio.run(main())
        self.assertEqual((good["reachable"], good["ip"], good["type"]), (True, HOST, "RA"))
        self.assertEqual(other.client.ip, "192.0.2.1")  # untouched
        self.assertEqual((refused["reachable"], refused["ip"]), (False, HOST))
        self.assertTrue(refused["error"].startswith("network error"))


class TestConfigSave(unittest.TestCase):
    def test_running_cycle_keeps_its_rule_and_the_next_one_uses_the_saved(self):
        w = World("day")
        app = w.app(shadow=True)
        w.run(app, 5 * 60 * 1000)
        self.assertEqual(app.controller.state, t.RUNNING)
        status, body = app.save_profiles(day_short(config.default_profiles()))
        self.assertEqual((status, body), (200, {"ok": True, "warnings": []}))
        self.assertEqual(app.controller.profiles, dict(config.default_profiles(), active="day"))  # not yet
        w.run(app, 4 * 60 * 60 * 1000)
        starts, phases = event_times(w, t.EV_CYCLE_START), event_times(w, t.EV_PHASE)
        # The first cycle (humid, from 1:00) runs its 60 min dry phase; the next one (after the cooldown) 10 min.
        self.assertEqual(phases[0] - starts[0], 60 * 60 * 1000)
        second = [p for p in phases if p > starts[1]][0]
        self.assertEqual(second - starts[1], 10 * 60 * 1000)
        self.assertEqual(app.controller.profiles["profiles"]["day"]["rules"][1]["phases"][0]["max_min"], 10)
        self.assertEqual(w.store.load("profiles"), dict(day_short(config.default_profiles()), active="day"))

    def test_saved_document_is_on_flash_at_once_and_logged(self):
        w = World("day")
        app = w.app(shadow=True)
        app.save_settings(dict(config.default_settings(HOST), poll_s=120))
        app.save_profiles(day_short(config.default_profiles()))
        self.assertEqual(w.store.load("settings")["poll_s"], 120)
        self.assertEqual(app.profiles_doc(), dict(day_short(config.default_profiles()), active="day"))
        self.assertEqual(app.settings_doc()["poll_s"], 120)
        self.assertTrue(app.wake.is_set())
        saved = [(d["kind"], d["msg"]) for d in flash(w.store) if d["kind"] == t.EV_CONFIG_SAVED]
        self.assertEqual(saved, [(t.EV_CONFIG_SAVED, "settings saved"), (t.EV_CONFIG_SAVED, "profiles saved")])
        w.run(app, 1000)
        self.assertEqual((app.poll_ms(), app.pending), (120 * 1000, {}))

    def test_errors_save_nothing(self):
        w = World("day")
        app = w.app(shadow=True)
        doc = config.default_profiles()
        doc["profiles"]["day"]["rules"][0]["phases"][0]["target_c"] = 31
        status, body = app.save_profiles(doc)
        self.assertEqual(status, 400)
        self.assertEqual(body["errors"][0]["path"], "profiles.day.rules[0].phases[0].target_c")
        status, body = app.save_settings(dict(config.default_settings(HOST), hard_cap_min=60))
        self.assertEqual((status, body["errors"][0]["path"]), (400, "hard_cap_min"))  # day.humid needs 80 min
        self.assertEqual((app.pending, w.store.load("settings")), ({}, None))
        self.assertEqual(w.store.load("profiles")["profiles"], config.default_profiles()["profiles"])

    def test_active_is_kept_and_cannot_be_deleted(self):
        w = World("night")
        app = w.app(shadow=True)
        doc = config.default_profiles()  # says "off"
        self.assertEqual(app.validate_profiles(doc)[0], 200)
        self.assertEqual(doc["active"], "night")
        del doc["profiles"]["night"]
        status, body = app.save_profiles(doc)
        self.assertEqual((status, body["errors"]), (400, [{"path": "profiles.night",
                                                           "msg": "the active profile; switch first"}]))

    def test_validate_does_not_save(self):
        w = World("day")
        app = w.app(shadow=True)
        doc = day_short(config.default_profiles())
        doc["profiles"]["day"]["rules"][1]["phases"].pop()
        status, body = app.validate_profiles(doc)
        self.assertEqual((status, [x["path"] for x in body["warnings"]]), (200, ["profiles.day.rules[1].phases"]))
        self.assertEqual((app.pending, app.warnings), ({}, []))
        self.assertEqual(w.store.load("profiles")["profiles"], config.default_profiles()["profiles"])

    def test_reset_with_a_custom_active_profile_switches_to_off(self):
        w = World()
        doc = config.default_profiles()
        doc["profiles"]["mine"] = doc["profiles"]["day"]
        doc["active"] = "mine"
        w.store.save("profiles", doc)
        app = w.app(shadow=True)
        w.run(app, 5 * 60 * 1000)
        self.assertEqual((app.controller.active, app.controller.state), ("mine", t.RUNNING))
        self.assertEqual(app.reset_profiles()[0], 200)
        self.assertEqual(w.store.load("profiles"), config.default_profiles())
        w.run(app, 6 * 60 * 1000)
        self.assertEqual(app.controller.active, "off")
        self.assertEqual((app.controller.state, app.controller.cycle["phase"]), (t.RUNNING, 1))  # finish: fan, off
        self.assertEqual(w.store.load("profiles")["active"], "off")

    def test_reset_racing_a_tick_leaves_a_valid_profiles_json(self):
        # The reset lands while a tick awaits its read; that tick then persists its snapshot, still on "mine".
        w = World()
        doc = config.default_profiles()
        doc["profiles"]["mine"] = doc["profiles"]["day"]
        doc["active"] = "mine"
        w.store.save("profiles", doc)
        app = w.app(shadow=True)
        w.run(app, 5 * 60 * 1000)
        app.reset_profiles()
        app._persist(app.controller._snapshot(app._now()))
        self.assertEqual(w.store.load("profiles"), config.default_profiles())

    def test_flash_full_saves_nothing(self):
        def full(name, doc):
            raise OSError(28)

        w = World("day")
        app = w.app(shadow=True)
        w.run(app, 1000)
        app.wake.clear()
        w.store.save = full
        failed = (500, {"ok": False, "errors": [{"path": "$", "msg": "flash write failed"}]})
        self.assertEqual(app.save_settings(dict(config.default_settings(HOST), poll_s=120)), failed)
        self.assertEqual(app.save_profiles(day_short(config.default_profiles())), failed)
        self.assertEqual(app.reset_profiles(), failed)
        self.assertEqual((app.pending, app.wake.is_set()), ({}, False))
        self.assertEqual([d for d in flash(w.store) if d["kind"] == t.EV_CONFIG_SAVED], [])

    def test_reset_keeps_a_default_active_profile(self):
        w = World("night")
        app = w.app(shadow=True)
        doc = config.default_profiles()
        doc["profiles"]["night"]["label"] = "Late"
        app.save_profiles(doc)
        app.reset_profiles()
        self.assertEqual(w.store.load("profiles"), dict(config.default_profiles(), active="night"))

    def test_switch_while_a_save_waits_keeps_the_saved_document(self):
        # A tick that switches the profile rewrites profiles.json: it must start from the saved document.
        w = World()
        app = w.app(shadow=True)
        app.save_profiles(day_short(config.default_profiles()))
        app._persist(dict(app.controller._snapshot(app._now()), profile="day"))  # a tick already under way
        self.assertEqual(w.store.load("profiles"), dict(day_short(config.default_profiles()), active="day"))

    def test_shadow_keeps_the_owner_dry_run_in_the_file(self):
        w = World()
        app = w.app(shadow=True)
        app.save_settings(dict(config.default_settings(HOST), dry_run=False, manual_hold_min=60))
        w.run(app, 1000)
        self.assertEqual((app.controller.settings["dry_run"], app.controller.settings["manual_hold_min"]), (True, 60))
        self.assertEqual((app.settings_doc()["dry_run"], w.store.load("settings")["dry_run"]), (False, False))
        self.assertTrue(app.status()["dry_run"])

    def test_new_ac_address_starts_over(self):
        w = World()
        app = w.app(shadow=True)
        w.run(app, 1000)
        client = app.client
        self.assertTrue(app.seen_ac)
        app.save_settings(dict(config.default_settings(HOST), poll_s=90))
        app._configure([])
        self.assertIs(app.client, client)  # same address and limits: kept
        app.save_settings(dict(config.default_settings(HOST), ac_ip="192.0.2.10"))
        app._configure([])
        self.assertEqual((app.client.ip, app.seen_ac, app.unreachable_since), ("192.0.2.10", False, None))
        app.save_settings(dict(config.default_settings(HOST), sensor_limits={"temp": [5, 50], "hum": [5, 100]}))
        w.run(app, 2000)
        self.assertEqual((app.client.ip, app.client.limits["temp"], app.seen_ac), (HOST, [5, 50], True))

    def test_saves_clear_the_load_warning(self):
        w = World()
        w.store.save("profiles", {"version": 1})
        app = w.app(shadow=True)
        self.assertEqual([x["path"] for x in app.warnings], ["profiles"])
        doc = config.default_profiles()
        doc["profiles"]["day"]["rules"][0]["phases"].pop()
        app.save_profiles(doc)
        self.assertEqual([x["path"] for x in app.warnings], ["profiles.profiles.day.rules[0].phases"])
        app.save_profiles(config.default_profiles())
        self.assertEqual((app.warnings, app.status()["config_warning"]), ([], None))

    def test_observe_mode_saves_settings_only(self):
        w = World()
        app = w.app(observe=True)
        self.assertEqual(app.save_settings(dict(config.default_settings(HOST), poll_s=30))[0], 200)
        self.assertEqual(app.save_profiles(config.default_profiles())[0], 409)
        self.assertEqual((app.profiles_doc(), app.reset_profiles()[0]), (None, 409))
        w.run(app, 1000)
        self.assertEqual(app.poll_ms(), 30 * 1000)
