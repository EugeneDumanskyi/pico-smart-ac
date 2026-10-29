# SPDX-License-Identifier: GPL-3.0-only
# Local wall time, only to pick a time-of-day stage; every duration still runs on ticks (ADR 0004, 0006).
# Pure: time is passed in as monotonic ms and only compared through ticks_since.
#
# Sources: the AC's last_ope.time (local time with a fake "Z") is the main one. It is the time of the last
# operation, so the clock syncs only when it changes between two reads, never from the value seen first after a
# boot. After our own write the change shows on the read-back, ~5 s late; after a remote press, up to one poll
# late. The phone's clock (the web page) is the backup, taken only until the first AC sync.
#
# A sync more than JUMP_S from the running clock is suspect: the time becomes unknown (the default stage runs) until
# a later sync agrees with it within AGREE_S. Wall seconds count from 2000-01-01 00:00 local; no time zone.
from smartac.core.types import ticks_since

AC = "ac"
PHONE = "phone"
MIN_YEAR = 2024  # an adapter that lost its clock reads as some early date
JUMP_S = 2 * 3600
AGREE_S = 10 * 60
REBASE_MS = 24 * 60 * 60 * 1000  # a stamp is only good for half a ticks period (about 6 days on the Pico)
DAY_S = 24 * 3600
_DIGITS = (0, 1, 2, 3, 5, 6, 8, 9, 11, 12, 14, 15, 17, 18)
_SEPS = ((4, "-"), (7, "-"), (10, "T"), (13, ":"), (16, ":"), (19, "Z"))
_MONTH_DAYS = (31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)


def days(y, m, d):
    # Days from 2000-01-01 to y-m-d (proleptic Gregorian, days-from-civil).
    if m <= 2:
        y -= 1
    era = y // 400
    yoe = y - era * 400
    doy = (153 * (m - 3 if m > 2 else m + 9) + 2) // 5 + d - 1
    return era * 146097 + yoe * 365 + yoe // 4 - yoe // 100 + doy - 730425


def parse(text):
    # "2026-10-28T09:32:02Z" -> wall seconds, or None if it isn't one (or is before MIN_YEAR).
    if not isinstance(text, str) or len(text) != 20:
        return None
    for i, ch in _SEPS:
        if text[i] != ch:
            return None
    for i in _DIGITS:
        if not "0" <= text[i] <= "9":
            return None
    y, mo, d = int(text[0:4]), int(text[5:7]), int(text[8:10])
    h, mi, s = int(text[11:13]), int(text[14:16]), int(text[17:19])
    if y < MIN_YEAR or not 1 <= mo <= 12 or not 1 <= d <= _MONTH_DAYS[mo - 1] or h > 23 or mi > 59 or s > 59:
        return None
    return days(y, mo, d) * DAY_S + h * 3600 + mi * 60 + s


def hms(wall):
    s = wall % DAY_S
    return "%02d:%02d:%02d" % (s // 3600, s // 60 % 60, s % 60)


def _gap(s):
    # Seconds between two clocks, for an event: "+4 s", "-3 min", "+6.2 h".
    a = abs(s)
    sign = "-" if s < 0 else "+"
    if a < 120:
        return "%s%d s" % (sign, a)
    if a < 7200:
        return "%s%d min" % (sign, a // 60)
    return "%s%d.%d h" % (sign, a // 3600, a % 3600 * 10 // 3600)


class Clock:
    def __init__(self):
        self.wall = None  # wall seconds at tick `at`; None: time unknown
        self.at = None
        self.source = None
        self.seen = None  # the last last_ope.time read; a change is a new operation
        self.suspect = None  # (wall, at) of a sync that disagreed with the running clock

    def now_s(self, now):
        # -> wall seconds now, or None. Rebases once a day; the sub-second rest is dropped (< 1 s a day).
        if self.wall is None:
            return None
        d = ticks_since(now, self.at)
        if d < 0 or d >= REBASE_MS:
            self.wall += max(d, 0) // 1000
            self.at = now
            d = 0
        return self.wall + d // 1000

    def minute(self, now):
        # -> minute of the day (0..1439), or None while the time is unknown.
        wall = self.now_s(now)
        return None if wall is None else wall % DAY_S // 60

    def status(self, now):
        wall = self.now_s(now)
        return {"time": None if wall is None else hms(wall)[:5], "source": self.source if wall is not None else None}

    def observe(self, now, op):
        # A reachable read's last_op ((time text, type) or None) -> an event message, or None if nothing happened.
        if op is None:
            return None
        text, kind = op
        if self.seen is None or text == self.seen:
            self.seen = text
            return None
        self.seen = text
        wall = parse(text)
        if wall is None:
            return "AC time unreadable: %r" % (text if isinstance(text, str) else None,)
        return "AC time %s (type %s, %s)" % (hms(wall), kind, self._sync(now, wall, AC))

    def phone(self, now, wall):
        # The page's local time, in wall seconds -> (event message or None, phone minus clock in s or None).
        # Taken only while the AC hasn't set the clock; otherwise it is only compared.
        running = self.now_s(now)
        diff = None if running is None else wall - running
        if running is not None and (self.source == AC or abs(diff) < 60):
            return None, diff
        return "phone time %s (%s)" % (hms(wall), self._sync(now, wall, PHONE)), diff

    def _sync(self, now, wall, source):
        # Take `wall` as the time now, unless it is suspect. -> what happened, for the event.
        ref = self.now_s(now)
        if ref is None and self.suspect is not None:
            s_wall, s_at = self.suspect
            if abs(wall - (s_wall + ticks_since(now, s_at) // 1000)) > AGREE_S:
                self.suspect = (wall, now)
                return "disagrees with the last; time unknown"
        if ref is not None and abs(wall - ref) > JUMP_S:
            self.wall = self.at = self.source = None
            self.suspect = (wall, now)
            return "%s off; time unknown" % _gap(wall - ref)
        self.wall, self.at, self.source, self.suspect = wall, now, source, None
        return "clock set" if ref is None else "clock was %s" % _gap(ref - wall)
