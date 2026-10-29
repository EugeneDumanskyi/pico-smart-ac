# SPDX-License-Identifier: GPL-3.0-only
# A long simulated run, generated upfront from a seed (sim/harness.py runs it): the device script (what the room
# and the people at the remote do, sim/device.py format) and the runner timeline (what the user does in the app,
# and power cuts). The run starts at 00:00 on day 0 and ends with a quiet tail in the "off" profile, so everything
# must settle: the remote switches the AC off, nothing else happens for TAIL_MIN.
#
# Timeline entries: (second, kind, arg)
#   ("action", {...})           POST /api/action or /api/active
#   ("reboot", {"down_s", "before_wrap_min", "after_write"})   power cut: at once, or right after the next write;
#                                down for down_s, the new boot's ticks start before_wrap_min before the wrap
import math

from smartac.core.types import FAN_AUTO, FANS, MODE_COOL, MODE_DRY, MODE_FAN, MODE_HEAT, ACState

DAY_MIN = 24 * 60
TAIL_MIN = 6 * 60
PROFILE_RULES = {"day_morning": 2, "night_evening": 2, "away": 1}  # rules per default rule profile, for "run"

# Expected events per day.
REMOTE_PER_DAY = 3
OUTAGE_PER_DAY = 1
REJECT_PER_DAY = 0.5
IGNORE_PER_DAY = 0.3
HUMID_PER_DAY = 2
ACTION_PER_DAY = 1
REBOOT_PER_DAY = 0.4


class Rng:
    # xorshift32: the same numbers on every port and version, unlike the random module.
    def __init__(self, seed):
        self.x = (seed * 2654435761 + 1) & 0xFFFFFFFF or 1

    def next(self):
        x = self.x
        x ^= (x << 13) & 0xFFFFFFFF
        x ^= x >> 17
        x ^= (x << 5) & 0xFFFFFFFF
        self.x = x
        return x

    def below(self, n):
        return self.next() % n

    def between(self, lo, hi):
        return lo + self.below(hi - lo + 1)

    def chance(self, p):
        return self.next() < p * 0x100000000

    def choice(self, seq):
        return seq[self.below(len(seq))]

    def count(self, rate):
        # How many events today for `rate` per day: the whole part, plus one more with the fractional chance.
        n = int(rate)
        return n + (1 if self.chance(rate - n) else 0)


class Scenario:
    def __init__(self, seed, days):
        self.seed = seed
        self.days = days
        self.end_min = days * DAY_MIN
        self.script = []  # (minute, kind, arg) for FakeDaikin
        self.timeline = []  # (second, kind, arg) for the runner

    def lines(self):
        # Both lists merged in time order, one line each, for printing.
        out = [(m * 60, "device", k, a) for m, k, a in self.script]
        out += [(s, "runner", k, a) for s, k, a in self.timeline]
        out.sort(key=lambda e: e[0])
        return ["%3dd %02d:%02d:%02d  %-6s %s %r" % (
            s // 86400, s // 3600 % 24, s // 60 % 60, s % 60, who, k, a) for s, who, k, a in out]


def _remote(rng):
    kind = rng.choice(("off", "off", "cool", "cool", "dry", "fan", "heat"))
    if kind == "off":
        return ACState(False, rng.choice((MODE_COOL, MODE_DRY, MODE_FAN)), None, None)
    fan = rng.choice(FANS)
    if kind == "cool":
        return ACState(True, MODE_COOL, rng.between(44, 56) * 5, fan)  # 22.0-28.0
    if kind == "heat":
        return ACState(True, MODE_HEAT, rng.between(40, 50) * 5, fan)  # 20.0-25.0
    return ACState(True, MODE_DRY if kind == "dry" else MODE_FAN, None, fan)


def _outdoor(rng, hour):
    # 26 C at 03:00 up to 34 C at 15:00, a little noise.
    t = 30 + 4 * math.cos(2 * math.pi * (hour % 24 - 15) / 24) + (rng.below(11) - 5) / 10
    return min(max(t, 26), 34)


def _action(rng, at, out):
    kind = rng.choice(("pause", "pause", "pause_untimed", "run", "stop", "stop_now", "clear_manual"))
    cycle = rng.choice(("finish", "leave"))
    if kind == "pause":
        out.append((at, "action", {"action": "pause", "minutes": rng.between(10, 240), "cycle": cycle}))
    elif kind == "pause_untimed":
        out.append((at, "action", {"action": "pause", "minutes": None, "cycle": cycle}))
        out.append((at + rng.between(30, 360) * 60, "action", {"action": "resume"}))
    elif kind == "run":
        profile = rng.choice(tuple(PROFILE_RULES))
        out.append((at, "action", {"action": "run", "profile": profile, "rule": rng.below(PROFILE_RULES[profile])}))
    else:
        out.append((at, "action", {"action": kind}))


def generate(seed, days):
    rng = Rng(seed)
    scn = Scenario(seed, days)
    script = scn.script
    timeline = scn.timeline
    quiet = scn.end_min - TAIL_MIN  # random events stop here
    away = set()
    off_day = None
    if days >= 4:
        while len(away) < 2:
            away.add(rng.between(1, days - 2))
        while off_day is None or off_day in away:
            off_day = rng.between(1, days - 2)

    def minute(day):
        return day * DAY_MIN + rng.below(DAY_MIN)

    profile = "off"
    for day in range(days):
        base = day * DAY_MIN
        for hour in range(24):
            script.append((base + hour * 60, "set_room", {"outdoor": _outdoor(rng, hour)}))
        for _ in range(rng.count(HUMID_PER_DAY)):
            script.append((minute(day), "set_room", {"hum": rng.between(82, 92)}))
        for _ in range(rng.count(REMOTE_PER_DAY)):
            script.append((minute(day), "remote", _remote(rng)))
        for _ in range(rng.count(OUTAGE_PER_DAY)):
            long = rng.chance(0.5)  # past the hard cap: the only way to reach it with the default profiles
            script.append((minute(day), "unreachable", rng.between(121, 180) if long else rng.between(1, 30)))
        for _ in range(rng.count(REJECT_PER_DAY)):
            script.append((minute(day), "reject_writes", rng.between(1, 4)))
        for _ in range(rng.count(IGNORE_PER_DAY)):
            script.append((minute(day), "ignore_writes", rng.between(1, 2)))

        # Profiles by hand: night from the start, day at about 07:00, night at about 22:00.
        for at_min, name in ((0, "night"), (7 * 60, "day"), (22 * 60, "night")):
            if at_min == 0 and day > 0:
                continue
            if day in away:
                name = "away"
            elif day == off_day:
                name = "off"
            if name != profile:
                at = (base + at_min + rng.below(30)) * 60 + rng.below(60)
                cycle = rng.choice(("finish", "leave"))
                timeline.append((at, "action", {"action": "set_profile", "profile": name, "cycle": cycle}))
                profile = name

        for _ in range(rng.count(ACTION_PER_DAY)):
            _action(rng, minute(day) * 60 + rng.below(60), timeline)
        for _ in range(rng.count(REBOOT_PER_DAY)):
            arg = {
                "down_s": rng.between(5, 300),
                "before_wrap_min": rng.below(3 * DAY_MIN),
                "after_write": rng.chance(0.5),
            }
            timeline.append((minute(day) * 60 + rng.below(60), "reboot", arg))

    # Nothing random in the tail; the profile timeline still runs (it only switches to "off" there anyway).
    scn.script = [e for e in script if e[0] < quiet or e[1] == "set_room"]
    scn.timeline = [e for e in timeline if e[0] < quiet * 60]
    scn.script.append((quiet, "remote", ACState(False, MODE_COOL, None, FAN_AUTO)))
    scn.timeline.append((quiet * 60 + 60, "action", {"action": "set_profile", "profile": "off", "cycle": "finish"}))
    scn.timeline.append((quiet * 60 + 120, "action", {"action": "resume"}))
    scn.script.sort(key=lambda e: e[0])
    scn.timeline.sort(key=lambda e: e[0])
    return scn
