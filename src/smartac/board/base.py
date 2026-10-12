# SPDX-License-Identifier: GPL-3.0-only
# The Board interface: everything the shell needs from the hardware, so the app runs unchanged on the Pico
# (board/pico.py) and on the Unix port against the simulator (board/host.py). Time comes only from here.


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
        raise NotImplementedError

    def reset(self):
        raise NotImplementedError

    def reset_cause(self):
        # -> "power_on", "watchdog", "soft" or "unknown".
        raise NotImplementedError

    def link_up(self):
        # -> True while Wi-Fi is connected.
        raise NotImplementedError
