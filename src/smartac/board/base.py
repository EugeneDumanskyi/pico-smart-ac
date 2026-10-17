# SPDX-License-Identifier: GPL-3.0-only
# The Board interface: everything the shell needs from the hardware, so the app runs unchanged on the Pico
# (board/pico.py) and on the Unix port against the simulator (board/host.py). Time comes only from here.
import asyncio

# Reset causes: what the hardware reports, or the reason a deliberate reset() left behind.
POWER_ON = "power_on"
WATCHDOG = "watchdog"
UNKNOWN = "unknown"
WIFI_DOWN = "wifi_down"  # the link stayed down too long (shell/net.py)
AC_SILENT = "ac_silent"  # the AC answered earlier this boot, then not for too long with the link up
CRASH = "crash"  # an uncaught exception (shell/tasks.py)
DEPLOY = "deploy"  # tools/deploy.py


async def wait_event(event, ms):
    # Sleep up to ms in real time, or until `event` is set. -> True if the event was set first.
    if event is not None and event.is_set():
        return True
    ms = int(max(ms, 0))
    if event is None:
        await asyncio.sleep_ms(ms)
        return False
    try:
        await asyncio.wait_for_ms(event.wait(), ms)
    except asyncio.TimeoutError:
        return False
    return True


class Board:
    def ticks_ms(self):
        # Monotonic ms (wraps; compare with core.types.ticks_since only).
        raise NotImplementedError

    async def wait(self, event, ms):
        # Sleep up to ms, or until `event` (asyncio.Event) is set. -> True if the event was set first.
        # event=None is a plain sleep (-> False).
        raise NotImplementedError

    async def sleep_ms(self, ms):
        await self.wait(None, ms)

    def wdt_start(self, ms):
        raise NotImplementedError

    def wdt_feed(self):
        raise NotImplementedError

    def led(self, pattern):
        # "on", "off" or "error" (fast blink).
        raise NotImplementedError

    def reset(self, reason):
        # Record `reason` (one of the causes above) for the next boot's reset_cause(), then reset.
        raise NotImplementedError

    def reset_cause(self):
        # -> this boot's cause: a reason left by reset(), else "power_on", "watchdog" or "unknown".
        raise NotImplementedError

    def link_up(self):
        # -> True while Wi-Fi is connected.
        raise NotImplementedError

    def wifi_connect(self):
        # Start connecting (returns at once; link_up() says when it's done).
        raise NotImplementedError

    def wifi_disconnect(self):
        raise NotImplementedError

    def rssi(self):
        # -> dBm, or None while the link is down.
        raise NotImplementedError

    def heap(self):
        # -> (free bytes, largest free block in bytes or None). Call after gc.collect(); may cost ~10 ms.
        raise NotImplementedError

    def device_info(self):
        # -> {"fw": ..., "ip": ..., "rssi": ...} for /api/state.
        raise NotImplementedError
