# SPDX-License-Identifier: GPL-3.0-only
# Wiring: boot from the store, then the control task (architecture.md, "Tasks"). One tick: read the AC, step the
# controller, persist, write at most once, wait SETTLE_MS, read back. Only the control task talks to the AC;
# action() is the one way in for the HTTP layer. Exceptions propagate: shell/tasks.py logs and resets.
# Observe mode (M1) builds no controller: a tick reads the AC and records history, so nothing can write.
# Shadow mode (M3) runs the controller with dry_run forced on, whatever settings.json and state.json say. A write
# that still comes out is not sent: it becomes a `blocked` event.
# A saved config document (save_profiles(), save_settings()) is on flash at once and parked in `pending`; the next
# tick applies it before it reads, so only the control task touches the controller and the client.
import asyncio

from smartac.core import clock, config
from smartac.core.clock import Clock
from smartac.core.config import OFF
from smartac.core.controller import FINISH, Controller
from smartac.core.events import EventLog, to_line
from smartac.core.history import History
from smartac.core.types import (
    ACT_SET_PROFILE,
    CMD_EVENT,
    CMD_PERSIST,
    CMD_WRITE,
    EV_BLOCKED,
    EV_BOOT,
    EV_CLOCK,
    EV_CONFIG_SAVED,
    OBSERVE,
    Reading,
    ticks_since,
)
from smartac.shell.daikin import DaikinClient

SETTLE_MS = 5000  # write -> read-back
TEST_TIMEOUT_MS = 3000  # "Test connection" to an address other than the one in use
MAX_ACTIONS = 8  # queued actions; more are refused until the next tick takes them
MAX_CRASH_LINE = 120


def _c(tenths):
    return None if tenths is None else tenths / 10


def _shadow_snapshot(snap):
    # A restored cycle is finished in dry_run too, whatever it started as.
    if not isinstance(snap, dict) or not isinstance(snap.get("cycle"), dict):
        return snap
    snap = dict(snap)
    snap["cycle"] = dict(snap["cycle"], dry_run=True)
    return snap


def _save_failed():
    # A save the flash couldn't take (full): the page shows the "$" error at the top of the tab.
    return 500, {"ok": False, "errors": [{"path": "$", "msg": "flash write failed"}]}


def _crash_line(text):
    # The last line of a traceback is the exception itself, which is what /api/state shows.
    if not text:
        return None
    lines = [line for line in text.split("\n") if line.strip()]
    return lines[-1].strip()[:MAX_CRASH_LINE] if lines else None


class App:
    def __init__(self, board, store, ac_ip=None, ac_port=80, observe=False, shadow=False):
        # ac_ip: DAIKIN_IP from secrets.py, used until settings.json names one.
        self.board = board
        self.store = store
        self.ac_ip = ac_ip
        self.ac_port = ac_port
        self.observe = observe
        self.shadow = shadow
        self.warnings = []
        self.log_errors = 0  # event lines the flash refused (full)
        self.boot = self._count(store.next_boot)
        self.reset_cause = board.reset_cause()
        self.resets = self._count(lambda save: store.reset_counts(self.reset_cause, save))
        self.crash = _crash_line(store.load_crash())
        settings = self._load("settings", config.validate_settings, config.default_settings(ac_ip))
        self.settings = settings
        self.client = None
        self._set_client(settings)
        now = board.ticks_ms()
        self.controller = None
        self.active = None
        if not observe:
            profiles = self._load(
                "profiles",
                lambda doc: config.validate_profiles(doc, settings["hard_cap_min"]),
                config.default_profiles(),
            )
            snap = store.load("state")
            if shadow:
                settings = dict(settings, dry_run=True)
                snap = _shadow_snapshot(snap)
            self.controller = Controller(profiles, settings, now, snap)
            self.active = profiles.get("active")  # as stored in profiles.json
        self.history = History()
        self.events = EventLog(self.boot)
        self.clock = Clock()  # wall time from the AC's last operation, the page's as backup; picks the stage
        self.phone_s = None  # the page's clock minus ours at its last POST /api/clock, in s
        self.queue = []
        self.pending = {}  # name -> a saved document the next tick applies
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

    def _count(self, count):
        # A boot or reset counter. One the flash can't take (full) stays in RAM: the board runs on (a reset here
        # would loop), the page shows the warning, and the next boot counts from the saved value again.
        try:
            return count(True)
        except OSError:
            if not any(w["path"] == "boot" for w in self.warnings):
                self.warnings.append({"path": "boot", "msg": "flash write failed, counters not saved"})
            return count(False)

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
        ctl = self.controller
        s = {"state": OBSERVE, "profile": None} if ctl is None else ctl.status(now)
        profiles = {} if ctl is None else ctl.profiles.get("profiles", {})
        r = self.last_reading
        ac = None if r is None else r.ac
        w = self.warnings
        device = {
            "uptime_s": self.uptime_ms // 1000,
            "boot": self.boot,
            "reset_cause": self.reset_cause,
            "resets": self.resets,
            "last_crash": self.crash,
            "log_errors": self.log_errors,
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
                "clock": self._clock(now),
                "dry_run": self._settings()["dry_run"],
                "poll_s": self._settings()["poll_s"],
                "shadow": self.shadow,
                "profiles": [
                    {
                        "name": k,
                        "label": profiles[k].get("label", k),
                        "rules": [r.get("name") for r in profiles[k].get("rules", [])],
                        "schedule": "stages" in profiles[k],
                    }
                    for k in sorted(profiles)
                ],
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

    def phone_time(self, text):
        # POST /api/clock: the page's local time as "2026-10-28T09:32:02Z" -> (status, body).
        wall = clock.parse(text)
        if wall is None:
            return 400, {"error": "time must be YYYY-MM-DDTHH:MM:SSZ, local, from %d on" % clock.MIN_YEAR}
        now = self._now()
        msg, self.phone_s = self.clock.phone(now, wall)
        if msg is not None:
            self._event(now, EV_CLOCK, msg)
        return 200, self._clock(now)

    async def test_ac(self, ip=None):
        # Settings tab, "Test connection": read the adapter's info at `ip` (None: the address in use). -> the reply.
        # The address in use goes through the running client, whose lock queues the test behind the control task's
        # read or write, so it can't make one fail; another address gets a throwaway client.
        client = self.client
        if ip is None:
            ip = None if client is None else client.ip
        if ip is None:
            return {"reachable": False, "error": "no AC address", "ip": None}
        if client is None or client.ip != ip:
            client = DaikinClient(ip, self.ac_port, timeout_ms=TEST_TIMEOUT_MS)
        start = self.board.ticks_ms()
        info, err = await client.info()
        if err is not None:
            return {"reachable": False, "error": err, "ip": ip}
        reply = {"reachable": True, "ip": ip, "ms": ticks_since(self.board.ticks_ms(), start)}
        reply.update(info)
        return reply

    def unreachable_ms(self):
        # -> how long the AC has failed to answer, or None while it answers (or before the first read).
        if self.unreachable_since is None:
            return None
        self._now()
        return self.uptime_ms - self.unreachable_since

    def history_chunks(self, minutes):
        self._now()
        return self.history.chunks(self.uptime_ms, minutes)

    def event_rows(self, limit):
        # /api/events: this boot's ring, newest first. limit: the query value, any garbage -> 100.
        try:
            limit = min(max(int(limit), 1), self.events.size)
        except (TypeError, ValueError):
            limit = 100
        return self.events.export(self._now(), limit)

    def poll_ms(self):
        return self._settings()["poll_s"] * 1000

    def event(self, kind, msg):
        # An event from another task (housekeeping).
        self._event(self._now(), kind, msg)

    def log(self, kind, msg):
        # To events.log only, not the RAM ring: the hourly heap line (housekeeping) would crowd real events out of it.
        self._now()
        temp, hum = self._sensors()
        self._append(to_line(self.events.boot, self.uptime_ms, kind, msg, temp, hum))

    # --- config documents (idea.md, "HTTP API": /api/profiles, /api/settings)
    # The handlers return (status, body). A save validates, writes the file (FileStore: .tmp, .bak, rename) and
    # parks the document for the next tick. A running cycle keeps the copy it took at its start.

    def profiles_doc(self):
        # -> profiles.json as the next tick will run it, with the controller's active profile; None in observe mode.
        ctl = self.controller
        if ctl is None:
            return None
        return dict(self.pending.get("profiles") or ctl.profiles, active=ctl.active)

    def settings_doc(self):
        # -> settings.json as saved: in shadow mode the owner's dry_run, not the forced one.
        return self.pending.get("settings") or self.settings

    def validate_profiles(self, doc):
        return self._check_profiles(doc)[:2]

    def save_profiles(self, doc, reset=False):
        status, body, warnings = self._check_profiles(doc, reset)
        if status == 200 and not self._save("profiles", doc, warnings):
            return _save_failed()
        return status, body

    def reset_profiles(self):
        # Restore defaults: the active profile stays if it is a default one; otherwise the tick that applies the
        # defaults switches to off (finish).
        return self.save_profiles(config.default_profiles(), True)

    def save_settings(self, doc):
        profiles = self.pending.get("profiles") or (None if self.controller is None else self.controller.profiles)
        errors, warnings = config.validate_settings(doc, profiles)
        if errors:
            return 400, {"ok": False, "errors": errors}
        if not self._save("settings", doc, warnings):
            return _save_failed()
        return 200, {"ok": True, "warnings": warnings}

    def _check_profiles(self, doc, reset=False):
        # -> (status, body, warnings). `active` is not the page's to change: it becomes the controller's current
        # profile, which a save can't delete or rename (a reset saves "off" instead).
        ctl = self.controller
        if ctl is None:
            return 409, {"ok": False, "errors": [{"path": "$", "msg": "no controller in observe mode"}]}, []
        profiles = doc.get("profiles") if isinstance(doc, dict) else None
        gone = ctl.active != OFF and isinstance(profiles, dict) and ctl.active not in profiles
        if isinstance(doc, dict):
            doc["active"] = OFF if gone and reset else ctl.active
        errors, warnings = config.validate_profiles(doc, self.settings_doc()["hard_cap_min"])
        if gone and not reset:
            errors = [e for e in errors if e["path"] != "active"]
            errors.append({"path": "profiles." + ctl.active, "msg": "the active profile; switch first"})
        if errors:
            return 400, {"ok": False, "errors": errors}, warnings
        return 200, {"ok": True, "warnings": warnings}, warnings

    def _save(self, name, doc, warnings):
        # -> False when the flash write fails (full): nothing changes. self.active stays as the controller runs
        # until the tick applies the document, or a tick under way would persist a profile the saved one lacks.
        try:
            self.store.save(name, doc)
        except OSError:
            return False
        self.pending[name] = doc
        prefix = name + "."
        self.warnings = [w for w in self.warnings if w["path"] != name and not w["path"].startswith(prefix)]
        self.warnings.extend({"path": prefix + w["path"], "msg": w["msg"]} for w in warnings)
        self._event(self._now(), EV_CONFIG_SAVED, name + " saved")
        self.wake.set()
        return True

    def _configure(self, actions):
        # The saved documents, applied at the start of a tick. A profile switch it needs goes first in `actions`.
        pending = self.pending
        self.pending = {}
        settings = pending.get("settings")
        if settings is not None:
            self.settings = settings
            self._set_client(settings)
        ctl = self.controller
        if ctl is None:
            return
        if settings is not None and self.shadow:
            settings = dict(settings, dry_run=True)
        ctl.configure(pending.get("profiles"), settings)
        if ctl.active != OFF and ctl.active not in ctl.profiles.get("profiles", {}):
            # Gone (Restore defaults, or a switch that raced a save): fail towards off.
            actions.insert(0, {"action": ACT_SET_PROFILE, "profile": OFF, "cycle": FINISH})

    def _set_client(self, settings):
        # A new address starts over: "unreachable" until the AC answers there, never the net task's reset, which
        # needs an AC that answered this boot.
        ip = settings.get("ac_ip") or self.ac_ip
        old = self.client
        limits = settings["sensor_limits"]
        if old is not None and old.ip == ip and old.limits == limits:
            return
        self.client = None if ip is None else DaikinClient(ip, self.ac_port, limits=limits)
        if old is not None and old.ip != ip:
            self.seen_ac = False
            self.unreachable_since = None

    def _sensors(self):
        r = self.last_reading
        return (None, None) if r is None else (r.temp, r.hum)

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
        actions = self.queue
        self.queue = []
        self.wake.clear()
        if self.pending:
            self._configure(actions)
        if self.controller is None:
            await self._read()
            return
        reading = await self._read()
        now = self._now()
        write = self._apply(now, self._step(now, reading, actions))
        if write is None:
            return
        if self.shadow:
            # Never sent, and not counted: writes stays 0, the read-only proof.
            self._event(now, EV_BLOCKED, "not sent: %r" % write)
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
        return self.controller.step(now, reading, actions, readback, self.clock.minute(now))

    async def _read(self):
        if self.client is None:
            reading = Reading.unreachable("no AC address")
        else:
            reading = await self.client.read()
        self.reads += 1
        if reading.ac is None:
            self.read_errors += 1
        self.last_reading = reading
        now = self._now()
        self.history.add(self.uptime_ms, reading)
        if reading.ac is not None:
            self.seen_ac = True
            self.unreachable_since = None
            msg = self.clock.observe(now, reading.last_op)
            if msg is not None:
                self._event(now, EV_CLOCK, msg)
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
            # The active profile lives in profiles.json; the snapshot only mirrors it. A saved document the next
            # tick applies is the one on flash, so it is the one to update.
            profiles = dict(self.pending.get("profiles") or self.controller.profiles)
            profiles["active"] = snap["profile"]
            self.store.save("profiles", profiles)
            self.active = snap["profile"]

    def _clock(self, now):
        s = self.clock.status(now)
        s["phone_s"] = self.phone_s
        return s

    def _event(self, now, kind, msg):
        # Each event carries the last reading: the one this step decided on.
        temp, hum = self._sensors()
        line = self.events.add(now, self.uptime_ms, kind, msg, temp, hum)
        if line is not None:
            self._append(line)

    def _append(self, line):
        # A line the flash refuses (full) is counted, and the RAM ring keeps the event.
        if not self.store.append_event(line):
            self.log_errors += 1

    def _now(self):
        # Board ticks; uptime is summed tick to tick, so it survives the ticks wrap.
        now = self.board.ticks_ms()
        self.uptime_ms += ticks_since(now, self._last)
        self._last = now
        return now
