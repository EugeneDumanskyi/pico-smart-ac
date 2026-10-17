# SPDX-License-Identifier: GPL-3.0-only
# Wi-Fi supervisor (architecture.md, "Failure handling"), one step every PERIOD_MS:
# - Link down: connect at once, then with backoff 5 s -> 60 s; reset after DOWN_RESET_MS down.
# - Link up but the AC silent: reconnect Wi-Fi once at AC_RECONNECT_MS; reset at AC_RESET_MS, but only if the AC
#   answered earlier this boot (that points to a silent Wi-Fi driver). An AC that never answered may just have a
#   new address (no DHCP reservation), and resetting would not fix that: the page shows "unreachable" instead.
#   The AC's silence counts from when the link came up, so a Wi-Fi outage doesn't trigger a reconnect at once.
# Pure timing over the board and the app (app.seen_ac, app.unreachable_ms(), app.stats), so it runs on the host.
from smartac.board.base import AC_SILENT, WIFI_DOWN
from smartac.core.types import ticks_since

MIN_MS = 60 * 1000
PERIOD_MS = 5000
BACKOFF_MIN_MS = 5000
BACKOFF_MAX_MS = 60 * 1000
DOWN_RESET_MS = 15 * MIN_MS
AC_RECONNECT_MS = 10 * MIN_MS
AC_RESET_MS = 30 * MIN_MS


class NetSupervisor:
    def __init__(self, board, app):
        self.board = board
        self.app = app
        now = board.ticks_ms()
        self.beat = now  # last step, for the watchdog task
        self.up = False  # the link at the last step; down at boot
        self.ever_up = False
        self.down_since = now
        self.last_try = None
        self.wait = BACKOFF_MIN_MS
        self.up_since = None  # while the link has been up for less than AC_RESET_MS
        self.forced = False  # our own reconnect for a silent AC is under way
        self.ac_reconnected = False  # done for this silent spell
        self.drops = 0
        self.reconnects = 0
        self.ac_reconnects = 0
        self._publish()

    async def run(self):
        while True:
            self.step()
            await self.board.sleep_ms(PERIOD_MS)

    def step(self):
        b = self.board
        now = b.ticks_ms()
        self.beat = now
        if b.link_up():
            if not self.up:
                self._came_up(now)
            else:
                self.forced = False  # a forced reconnect that never showed the link down is over too
            silent = self._ac_silent_ms(now)
            self._check_ac(silent)
            b.led("off" if silent is None else "on")  # dark while healthy: lit only when something is wrong
        else:
            if self.up:
                self._went_down(now)
            if ticks_since(now, self.down_since) >= DOWN_RESET_MS:
                b.reset(WIFI_DOWN)
                self.down_since = now  # what a fresh boot would see (the host board returns from reset)
            elif self.last_try is None or ticks_since(now, self.last_try) >= self.wait:
                if self.last_try is not None:
                    self.wait = min(self.wait * 2, BACKOFF_MAX_MS)
                self.last_try = now
                b.wifi_connect()
            b.led("error")
        self._publish()

    def _came_up(self, now):
        if self.ever_up and not self.forced:
            self.reconnects += 1
        if not self.forced:
            self.up_since = now
        self.up = self.ever_up = True
        self.forced = False
        self.last_try = None
        self.wait = BACKOFF_MIN_MS

    def _went_down(self, now):
        self.up = False
        if not self.forced:
            self.drops += 1
        self.down_since = now

    def _ac_silent_ms(self, now):
        silent = self.app.unreachable_ms()
        if silent is None:
            self.ac_reconnected = False
            return None
        if self.up_since is not None:
            up = ticks_since(now, self.up_since)
            if up >= AC_RESET_MS:
                self.up_since = None  # up long enough; stop comparing before the ticks wrap
            else:
                silent = min(silent, up)
        return silent

    def _check_ac(self, silent):
        if silent is None:
            return
        if silent >= AC_RESET_MS and self.app.seen_ac:
            self.board.reset(AC_SILENT)
            self.up_since = self.board.ticks_ms()  # what a fresh boot would see (the host board returns)
        elif silent >= AC_RECONNECT_MS and not self.ac_reconnected:
            self.ac_reconnected = True
            self.ac_reconnects += 1
            self.forced = True
            self.board.wifi_disconnect()
            self.board.wifi_connect()

    def _publish(self):
        s = self.app.stats
        s["wifi_drops"] = self.drops
        s["wifi_reconnects"] = self.reconnects
        s["ac_reconnects"] = self.ac_reconnects
