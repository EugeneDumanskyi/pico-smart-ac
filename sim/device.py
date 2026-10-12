# SPDX-License-Identifier: GPL-3.0-only
# Fake Daikin indoor + outdoor unit (docs/DAIKIN_API.md). No I/O: handle(parsed body) -> response dict, or None
# when the unit is unreachable. Keeps raw params like the real unit, so each mode keeps its own target and fan.
# The room model and the script catch up with the clock lazily, on each call.
#
# Script: [(minute, kind, arg), ...], minutes after the device was created, fired in order as the clock passes:
#   ("remote", ACState)        an IR press: sets power, mode and that mode's target and fan; any mode
#   ("unreachable", minutes)   handle() returns None for that long
#   ("reject_writes", n)       the next n valid writes get rsc 4000 and are not applied
#   ("ignore_writes", n)       the next n valid writes get rsc 2004 but are not applied
#   ("set_room", {"temp": ..., "hum": ..., "outdoor": ...})   jump the room model
# The same are methods for direct use in tests.
import time

from sim.room import Room
from smartac.core import daikin_codec as codec
from smartac.core.types import MODE_UNKNOWN, ACState

MIN_MS = 60 * 1000

# Start state: off in fan mode, the way the owner usually leaves it. Targets in half degrees.
START_E3001 = {
    "p_01": "0000",
    "p_02": "34",  # cool 26.0
    "p_03": "2C",  # heat 22.0
    "p_1D": "30",  # auto 24.0
    "p_09": "0A00",
    "p_0A": "0A00",
    "p_26": "0A00",
    "p_27": "0A00",
    "p_28": "0500",
}
# Streamer (p_20) and the mold-proof candidates: readable, never writable here.
START_E3003 = {"p_20": "00", "p_1B": "00", "p_29": "00", "p_2C": "00", "p_30": "00", "p_36": "00"}

SETPOINT_PARAMS = tuple(codec.SETPOINT_PARAM.values())
FAN_PARAMS = tuple(codec.FAN_PARAM.values())
TARGET_RAW = (codec.TARGET_LIMITS[0] * 2, codec.TARGET_LIMITS[1] * 2)


def _leaf(pn, pv):
    return {"pn": pn, "pt": 1, "pv": pv}


def _node(pn, params):
    return {"pn": pn, "pt": 1, "pch": [_leaf(k, v) for k, v in params.items()]}


def _path(req):
    to = req.get("to") if isinstance(req, dict) else None
    return to.split("?")[0] if isinstance(to, str) else ""


def _valid(group, param, pv):
    # What the real unit accepted (docs/DAIKIN_API.md); anything else is rejected like the streamer write.
    if group == "e_A002":
        return param == "p_01" and pv in ("00", "01")
    if group != "e_3001":
        return False
    if param == "p_01":
        return pv in codec.MODE_CODES
    if param in SETPOINT_PARAMS:
        try:
            raw = codec.le_int(pv)
        except ValueError:
            return False
        return len(pv) == 2 and TARGET_RAW[0] <= raw <= TARGET_RAW[1]
    if param in FAN_PARAMS:
        return pv in codec.FAN_CODES
    return False


class FakeDaikin:
    def __init__(self, clock, room=None, script=None):
        self.clock = clock
        self.room = room or Room()
        self.start = clock()
        self.now = self.start  # clock time the room and script have caught up to
        self.script = sorted(script or [], key=lambda e: e[0])
        self.power = "00"
        self.e3001 = dict(START_E3001)
        self.e3003 = dict(START_E3003)
        self.down_until = None
        self.reject = 0
        self.ignore = 0
        self.writes = 0  # write requests received, valid or not

    # --- time

    def _advance(self, to):
        minutes = time.ticks_diff(to, self.now) / MIN_MS
        if minutes > 0:
            self.room.step(minutes, self.ac())
            self.now = to

    def sync(self):
        # Catch up with the clock: fire due script events in order, stepping the room between them.
        now = self.clock()
        while self.script:
            at = time.ticks_add(self.start, int(self.script[0][0] * MIN_MS))
            if time.ticks_diff(now, at) < 0:
                break
            _, kind, arg = self.script.pop(0)
            self._advance(at)
            self._fire(kind, arg)
        self._advance(now)

    def _fire(self, kind, arg):
        if kind == "remote":
            self._remote(arg)
        elif kind == "unreachable":
            self.down_until = time.ticks_add(self.now, int(arg * MIN_MS))
        elif kind == "reject_writes":
            self.reject = arg
        elif kind == "ignore_writes":
            self.ignore = arg
        elif kind == "set_room":
            self.room.set(**arg)
        else:
            raise ValueError("unknown script event %r" % (kind,))

    # --- script events as methods

    def remote(self, ac):
        self.sync()
        self._fire("remote", ac)

    def unreachable(self, minutes):
        self.sync()
        self._fire("unreachable", minutes)

    def reject_writes(self, n):
        self.sync()
        self._fire("reject_writes", n)

    def ignore_writes(self, n):
        self.sync()
        self._fire("ignore_writes", n)

    def set_room(self, temp=None, hum=None, outdoor=None):
        self.sync()
        self.room.set(temp, hum, outdoor)

    def _remote(self, ac):
        if ac.mode not in codec.MODE_VALUES:
            raise ValueError("remote can't set mode %r" % (ac.mode,))
        self.power = "01" if ac.power else "00"
        self.e3001["p_01"] = codec.MODE_VALUES[ac.mode]
        if ac.target is not None and ac.mode in codec.SETPOINT_PARAM:
            self.e3001[codec.SETPOINT_PARAM[ac.mode]] = codec.le_hex(ac.target // 5, 1)
        if ac.fan is not None and ac.mode in codec.FAN_PARAM:
            self.e3001[codec.FAN_PARAM[ac.mode]] = codec.FAN_VALUES[ac.fan]

    # --- state

    def ac(self):
        # The AC state as the app would decode it.
        mode = codec.MODE_CODES.get(self.e3001["p_01"], MODE_UNKNOWN)
        target = fan = None
        if mode in codec.SETPOINT_PARAM:
            target = codec.le_int(self.e3001[codec.SETPOINT_PARAM[mode]]) * 5
        if mode in codec.FAN_PARAM:
            fan = codec.FAN_CODES.get(self.e3001[codec.FAN_PARAM[mode]])
        return ACState(self.power == "01", mode, target, fan)

    def reachable(self):
        return self.down_until is None or time.ticks_diff(self.down_until, self.now) <= 0

    # --- requests

    def handle(self, body):
        # Parsed /dsiot/multireq body -> response dict, or None (unreachable: no answer at all).
        self.sync()
        if not self.reachable():
            return None
        self.down_until = None
        reqs = body.get("requests") if isinstance(body, dict) else None
        if isinstance(reqs, list) and reqs and all(isinstance(r, dict) and r.get("op") == 2 for r in reqs):
            return {"responses": [self._read(_path(r)) for r in reqs]}
        entries = reqs if isinstance(reqs, list) and reqs else [None]
        if any(isinstance(r, dict) and r.get("op") == 3 for r in entries):
            self.writes += 1
        rsc = self._write(body)
        return {"responses": [{"fr": _path(r), "rsc": rsc} for r in entries]}

    def _read(self, path):
        if path == codec.INDOOR:
            root = self._indoor()
        elif path == codec.OUTDOOR:
            root = self._outdoor()
        else:
            return {"fr": path, "rsc": codec.RSC_REJECTED}
        return {"fr": path, "pc": {"pn": "dgc_status", "pch": [root]}, "rsc": codec.RSC_READ_OK}

    def _indoor(self):
        s = self.room.sensors()
        sensors = {"p_01": codec.le_hex(min(max(s["temp"], -128), 127), 1), "p_02": codec.le_hex(s["hum"], 1)}
        return {
            "pn": "e_1002",
            "pt": 1,
            "pch": [
                _node("e_A002", {"p_01": self.power}),
                _node("e_3001", self.e3001),
                _node("e_3003", self.e3003),
                _node("e_A00B", sensors),
            ],
        }

    def _outdoor(self):
        out = min(max(self.room.sensors()["outdoor_half"], -32768), 32767)
        return {"pn": "e_1003", "pt": 1, "pch": [_node("e_A00D", {"p_01": codec.le_hex(out, 2)})]}

    def _write(self, body):
        # -> rsc. All or nothing: one bad param rejects the whole request. Accepted writes apply at once.
        params, _ = codec.parse_write(body)
        if params is None or not all(_valid(*p) for p in params):
            return codec.RSC_REJECTED
        if self.reject:
            self.reject -= 1
            return codec.RSC_REJECTED
        if self.ignore:
            self.ignore -= 1
            return codec.RSC_WRITE_OK
        for group, param, pv in params:
            if group == "e_A002":
                self.power = pv
            else:
                self.e3001[param] = pv
        return codec.RSC_WRITE_OK

