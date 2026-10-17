# SPDX-License-Identifier: GPL-3.0-only
# Wiring: boot from the store, then the control task (architecture.md, "Tasks"). One tick: read the AC, step the
# controller, persist, write at most once, wait SETTLE_MS, read back. Only the control task talks to the AC;
# action() is the one way in for the HTTP layer. Exceptions propagate: shell/tasks.py logs and resets.
# Observe mode (M1) builds no controller: a tick reads the AC and records history, so nothing can write.
import asyncio

from smartac.core import config
from smartac.core.controller import Controller
from smartac.core.events import EventLog, to_line
from smartac.core.history import History
from smartac.core.types import CMD_EVENT, CMD_PERSIST, CMD_WRITE, EV_BOOT, OBSERVE, Reading, ticks_since
from smartac.shell.daikin import DaikinClient

SETTLE_MS = 5000  # write -> read-back
MAX_ACTIONS = 8  # queued actions; more are refused until the next tick takes them
MAX_CRASH_LINE = 120


def _c(tenths):
    return None if tenths is None else tenths / 10


def _crash_line(text):
    # The last line of a traceback is the exception itself, which is what /api/state shows.
    if not text:
        return None
    lines = [line for line in text.split("\n") if line.strip()]
    return lines[-1].strip()[:MAX_CRASH_LINE] if lines else None


class App:
    def __init__(self, board, store, ac_ip=None, ac_port=80, observe=False):
        # ac_ip: DAIKIN_IP from secrets.py, used until settings.json names one.
        self.board = board
        self.store = store
        self.observe = observe
        self.boot = store.next_boot()
        self.reset_cause = board.reset_cause()
        self.resets = store.reset_counts(self.reset_cause)
        self.crash = _crash_line(store.load_crash())
        self.warnings = []
        settings = self._load("settings", config.validate_settings, config.default_settings(ac_ip))
        self.settings = settings
        ip = settings.get("ac_ip") or ac_ip
        self.client = None if ip is None else DaikinClient(ip, ac_port, limits=settings["sensor_limits"])
        now = board.ticks_ms()
        self.controller = None
        self.active = None
        if not observe:
            profiles = self._load(
                "profiles",
                lambda doc: config.validate_profiles(doc, settings["hard_cap_min"]),
                config.default_profiles(),
            )
            self.controller = Controller(profiles, settings, now, store.load("state"))
            self.active = profiles.get("active")  # as stored in profiles.json
        self.history = History()
        self.events = EventLog(self.boot)
        self.queue = []
        self.wake = asyncio.Event()
        self.uptime_ms = 0
        self._last = now
        self.beat = now  # start of the last tick, for the watchdog task
        self.seen_ac = False  # the AC answered at least once this boot
        self.unreachable_since = None  # uptime ms of the first failed read in a row
        self.stats = {}  # filled by the net and housekeeping tasks, shown under "device"
        self.last_reading = None
        self.reads = 0
        self.read_errors = 0
        self.writes = 0
        self.write_errors = 0
        self.last_write_error = None
        self._event(now, EV_BOOT, "boot %d (%s)" % (self.boot, self.reset_cause))

    def _load(self, name, validate, default):
        doc = self.store.load(name)
        if doc is None:
            return default
        errors, warnings = validate(doc)
        if errors:
            first = "%s: %s" % (errors[0]["path"], errors[0]["msg"])
            self.warnings.append({"path": name, "msg": "invalid (%s), using defaults" % first})
            return default
        for w in warnings:
            self.warnings.append({"path": "%s.%s" % (name, w["path"]), "msg": w["msg"]})
        return doc

    # --- the way in for the HTTP layer

    def action(self, a):
        # Queue an action for the next tick and wake the control task. -> False if the queue is full.
        if len(self.queue) >= MAX_ACTIONS:
            return False
        self.queue.append(a)
        self.wake.set()
        return True

    def status(self):
        # /api/state (idea.md, "HTTP API").
        now = self._now()
        s = {"state": OBSERVE, "profile": None} if self.controller is None else self.controller.status(now)
        r = self.last_reading
        ac = None if r is None else r.ac
        w = self.warnings
        device = {
            "uptime_s": self.uptime_ms // 1000,
            "boot": self.boot,
            "reset_cause": self.reset_cause,
            "resets": self.resets,
            "last_crash": self.crash,
        }
        device.update(self.board.device_info())
        device.update(self.stats)
        unreachable = self.unreachable_ms()
        s.update(
            {
                "room": {"temp": None if r is None else _c(r.temp), "hum": None if r is None else r.hum},
                "outdoor_temp": None if r is None else _c(r.outdoor_temp),
                "ac": {
                    "reachable": ac is not None,
                    "power": None if ac is None else ac.power,
                    "mode": None if ac is None else ac.mode,
                    "target_c": None if ac is None else _c(ac.target),
                    "fan": None if ac is None else ac.fan,
                },
                "dry_run": self._settings()["dry_run"],
                "last_error": None if r is None else r.error,
                "unreachable_s": None if unreachable is None else unreachable // 1000,
                "config_warning": "%s: %s" % (w[0]["path"], w[0]["msg"]) if w else None,
                "warnings": w,
                "reads": self.reads,
                "read_errors": self.read_errors,
                "writes": self.writes,
                "write_errors": self.write_errors,
                "last_write_error": self.last_write_error,
                "device": device,
            }
        )
        return s

    def unreachable_ms(self):
        # -> how long the AC has failed to answer, or None while it answers (or before the first read).
        if self.unreachable_since is None:
            return None
        self._now()
        return self.uptime_ms - self.unreachable_since

    def history_chunks(self, minutes):
        self._now()
        return self.history.chunks(self.uptime_ms, minutes)

    def poll_ms(self):
        return self._settings()["poll_s"] * 1000

    def event(self, kind, msg):
        # An event from another task (housekeeping).
        self._event(self._now(), kind, msg)

    def log(self, kind, msg):
        # To events.log only, not the RAM ring: the hourly heap line (housekeeping) would crowd real events out of it.
        self._now()
        self.store.append_event(to_line(self.events.boot, self.uptime_ms, kind, msg))

    def _settings(self):
        return self.settings if self.controller is None else self.controller.settings

    # --- the control task

    async def control(self):
        # Fixed cadence: a tick every poll_s from the start of the last one; an action wakes it early.
        while True:
            start = self.board.ticks_ms()
            self.beat = start
            await self.tick()
            left = self.poll_ms() - ticks_since(self.board.ticks_ms(), start)
            if left > 0:
                await self.board.wait(self.wake, left)

    async def tick(self):
        if self.controller is None:
            await self._read()
            return
        actions = self.queue
        self.queue = []
        self.wake.clear()
        reading = await self._read()
        now = self._now()
        write = self._apply(now, self._step(now, reading, actions))
        if write is None:
            return
        err = await self.client.write(write)
        self.writes += 1
        if err is not None:
            # Not fed to the core: the read-back shows whether anything changed.
            self.write_errors += 1
            self.last_write_error = err
        await self.board.sleep_ms(SETTLE_MS)
        reading = await self._read()
        now = self._now()
        self._apply(now, self._step(now, reading, (), True))

    def _step(self, now, reading, actions, readback=False):
        # The seam the simulator's runner overrides to watch every step.
        return self.controller.step(now, reading, actions, readback)

    async def _read(self):
        if self.client is None:
            reading = Reading.unreachable("no AC address")
        else:
            reading = await self.client.read()
        self.reads += 1
        if reading.ac is None:
            self.read_errors += 1
        self.last_reading = reading
        self._now()
        self.history.add(self.uptime_ms, reading)
        if reading.ac is not None:
            self.seen_ac = True
            self.unreachable_since = None
        elif self.unreachable_since is None:
            self.unreachable_since = self.uptime_ms
        return reading

    def _apply(self, now, cmds):
        # Events and the snapshot reach the store before the write goes out (ADR 0003): a reset in between
        # finds `written` in state.json. -> the state to write, or None.
        write = None
        for kind, payload in cmds:
            if kind == CMD_EVENT:
                self._event(now, payload[0], payload[1])
            elif kind == CMD_PERSIST:
                self._persist(payload)
            elif kind == CMD_WRITE:
                write = payload
        return write

    def _persist(self, snap):
        self.store.save("state", snap)
        if snap["profile"] != self.active:
            # The active profile lives in profiles.json; the snapshot only mirrors it.
            profiles = dict(self.controller.profiles)
            profiles["active"] = snap["profile"]
            self.store.save("profiles", profiles)
            self.active = snap["profile"]

    def _event(self, now, kind, msg):
        line = self.events.add(now, self.uptime_ms, kind, msg)
        if line is not None:
            self.store.append_event(line)

    def _now(self):
        # Board ticks; uptime is summed tick to tick, so it survives the ticks wrap.
        now = self.board.ticks_ms()
        self.uptime_ms += ticks_since(now, self._last)
        self._last = now
        return now
