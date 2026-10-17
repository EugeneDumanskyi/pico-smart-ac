# SPDX-License-Identifier: GPL-3.0-only
# Device test: an exception in a task is written to crash.txt and resets with cause "crash". Reads the AC once.
# In maintenance mode (tools/deploy.py --stay), run twice:  mpremote run tests/device/crash_check.py
# Uses the real store, so the crash shows in /api/state (last_crash, resets) afterwards.
import os

import secrets
from smartac.board.pico import PicoBoard
from smartac.shell import tasks
from smartac.shell.app import App
from smartac.shell.store import FileStore

MARKER = "/crash_check.txt"
MESSAGE = "crash_check: deliberate"


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
    print("reset_cause %s, resets %r, last_crash %r" % (d["reset_cause"], d["resets"], d["last_crash"]))
    print("crash.txt:\n" + (store.load_crash() or "(missing)"))
    ok = d["reset_cause"] == "crash" and d["last_crash"] == "RuntimeError: " + MESSAGE
    print("PASS" if ok else "FAIL")
else:

    class Crashing(App):
        async def tick(self):
            await super().tick()
            raise RuntimeError(MESSAGE)

    with open(MARKER, "w") as f:
        f.write("1")
    print("expect a crash and a reset after the first read, then run this script again")
    tasks.run(board, store, lambda: Crashing(board, store, secrets.DAIKIN_IP, observe=True))
