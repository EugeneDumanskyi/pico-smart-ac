# SPDX-License-Identifier: GPL-3.0-only
# The event ring (docs/idea.md, "History storage"; architecture.md): the last SIZE events in RAM for
# GET /api/events, and the line format of the flash event log. Pure: time is passed in as monotonic ms and
# only compared through ticks_since (ADR 0004).
#
# Ages: a stamp is only good for half a ticks period (about 6 days on the Pico), so each slot keeps
# base_ms + a stamp, and prune() moves the stamp up to now once it is REBASE_MS old. The shell calls prune()
# from its 60 s housekeeping, and add()/export() call it too, so no stamp gets near the wrap.
#
# Readings: each event keeps the room temp (tenths) and humidity of the step it was decided on, so a decision
# can be judged later. JSON (rows and lines) gives temp in degrees C, like /api/state.
#
# Flash: add() returns the line for events.log for the kinds in flash_kinds; store.py only appends and rotates.
# A line holds the boot number and uptime, no clock time (the age of an earlier boot's event is unknown).
# The decisions go to flash in every mode, so the History tab and a shadow review see them after a reboot.
import json

from smartac.core.types import (
    EV_BLOCKED,
    EV_BOOT,
    EV_CLOCK,
    EV_CONFIG_SAVED,
    EV_CYCLE_END,
    EV_CYCLE_START,
    EV_HARD_CAP,
    EV_MANUAL,
    EV_PAUSE,
    EV_PHASE,
    EV_PROFILE,
    EV_RESUME,
    EV_STAGE,
    EV_TAKEOVER,
    EV_UNREACHABLE,
    EV_WOULD,
    EV_WRITE_FAILED,
    ticks_since,
)

SIZE = 200
MAX_MSG = 80  # longer messages are cut, which bounds the ring's RAM
REBASE_MS = 24 * 60 * 60 * 1000
FLASH_KINDS = (
    EV_CYCLE_END,
    EV_MANUAL,
    EV_TAKEOVER,
    EV_WRITE_FAILED,
    EV_CONFIG_SAVED,
    EV_BOOT,
    EV_CYCLE_START,
    EV_PHASE,
    EV_WOULD,
    EV_HARD_CAP,
    EV_PROFILE,
    EV_UNREACHABLE,
    EV_BLOCKED,
    EV_PAUSE,
    EV_RESUME,
    EV_CLOCK,
    EV_STAGE,
)


def to_flash(kind, kinds=FLASH_KINDS):
    return kind in kinds


def _c(tenths):
    return None if tenths is None else tenths / 10


def to_line(boot, uptime_ms, kind, msg, temp=None, hum=None):
    # temp in tenths, hum in percent; None if the step had no reading.
    return json.dumps({"boot": boot, "up_s": uptime_ms // 1000, "kind": kind, "msg": msg, "temp": _c(temp),
                       "hum": hum})


def _num(v):
    return v is None or (isinstance(v, (int, float)) and not isinstance(v, bool))


def from_line(line):
    # A flash line -> an /api/events row (age_s is None: its boot's clock is gone), or None if it is corrupt.
    # Lines from before M3 have no temp and hum: they read as None.
    try:
        d = json.loads(line)
        row = {"age_s": None, "boot": d["boot"], "kind": d["kind"], "msg": d["msg"], "temp": d.get("temp"),
               "hum": d.get("hum")}
    except (ValueError, KeyError, TypeError, AttributeError):
        return None
    if not isinstance(row["boot"], int) or not isinstance(row["kind"], str) or not isinstance(row["msg"], str):
        return None
    if not _num(row["temp"]) or not _num(row["hum"]):
        return None
    return row


class EventLog:
    def __init__(self, boot, size=SIZE, flash_kinds=FLASH_KINDS):
        self.boot = boot
        self.size = size
        self.flash_kinds = flash_kinds
        # Parallel lists, allocated once: age = base_ms + ticks_since(now, stamp).
        self.stamp = [0] * size
        self.base_ms = [0] * size
        self.up_ms = [0] * size
        self.kind = [None] * size
        self.msg = [None] * size
        self.temp = [None] * size  # tenths
        self.hum = [None] * size
        self.head = 0  # the next slot to write
        self.count = 0

    def add(self, now, uptime_ms, kind, msg, temp=None, hum=None):
        # Store an event; returns its flash line, or None if this kind stays in RAM.
        self.prune(now)
        msg = msg[:MAX_MSG]
        i = self.head
        self.stamp[i] = now
        self.base_ms[i] = 0
        self.up_ms[i] = uptime_ms
        self.kind[i] = kind
        self.msg[i] = msg
        self.temp[i] = temp
        self.hum[i] = hum
        self.head = (i + 1) % self.size
        if self.count < self.size:
            self.count += 1
        if to_flash(kind, self.flash_kinds):
            return to_line(self.boot, uptime_ms, kind, msg, temp, hum)
        return None

    def prune(self, now):
        # Fold old stamps into base_ms. A negative age (a stamp from before a missed wrap) counts as 0.
        for i in range(self.count):  # slots fill from 0, so the used ones are 0..count-1
            d = ticks_since(now, self.stamp[i])
            if d < 0:
                self.stamp[i] = now
            elif d >= REBASE_MS:
                self.base_ms[i] += d
                self.stamp[i] = now

    def export(self, now, limit=100):
        # /api/events rows for this boot, newest first.
        self.prune(now)
        out = []
        for n in range(min(self.count, limit)):
            i = (self.head - 1 - n) % self.size
            age = self.base_ms[i] + ticks_since(now, self.stamp[i])
            out.append({"age_s": age // 1000, "boot": self.boot, "kind": self.kind[i], "msg": self.msg[i],
                        "temp": _c(self.temp[i]), "hum": self.hum[i]})
        return out
