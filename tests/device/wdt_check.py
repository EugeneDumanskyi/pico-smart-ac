# SPDX-License-Identifier: GPL-3.0-only
# Device test: a stalled control task ends in a watchdog reset with cause "watchdog". Reads the AC once, no writes.
# In maintenance mode (tools/deploy.py --stay), run twice:  mpremote run tests/device/wdt_check.py
# The first run starts the app with poll_s 5 and stops the control task's heartbeat after one tick: the watchdog task
# stops feeding after 2 x 5 s, the hardware resets ~8 s later. The second run prints the cause the app sees.
# Uses the real store, so the reset shows in /api/state's resets afterwards.
import asyncio
import os
import time

import secrets
from smartac.board.pico import PicoBoard
from smartac.shell import tasks
from smartac.shell.app import App
from smartac.shell.store import FileStore

MARKER = "/wdt_check.txt"


def exists(path):
    try:
        os.stat(path)
        return True
    except OSError:
        return False


board = PicoBoard(secrets)
store = FileStore("/")

if exists(MARKER):
    os.remove(MARKER)
    app = App(board, store, secrets.DAIKIN_IP, observe=True)
    d = app.status()["device"]
    print("reset_cause %s, resets %r" % (d["reset_cause"], d["resets"]))
    print("PASS" if d["reset_cause"] == "watchdog" else "FAIL: expected watchdog")
else:

    class Stalled(App):
        async def control(self):
            await self.tick()
            start = time.ticks_ms()
            while True:  # no more beats
                print("stalled %d s" % (time.ticks_diff(time.ticks_ms(), start) // 1000))
                await asyncio.sleep_ms(1000)

    def make():
        app = Stalled(board, store, secrets.DAIKIN_IP, observe=True)
        app.settings["poll_s"] = 5
        return app

    with open(MARKER, "w") as f:
        f.write("1")
    print("expect a reset after ~18 s, then run this script again")
    tasks.run(board, store, make)
