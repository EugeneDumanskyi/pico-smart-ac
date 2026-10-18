# SPDX-License-Identifier: GPL-3.0-only
# Host runner for work on the web page: micropython sim/dev.py <dir> [speed]
# A fake Daikin, the app in shadow mode with its files in <dir> (created if missing), and the API on
# 127.0.0.1:8080 serving web/ as it is in the repo, so a reload shows an edit. The token is "dev".
# speed scales time (ScaledClock): at 10, a 60 min phase takes 6 min. Ctrl-C stops it.
import asyncio
import os
import sys

ROOT = (__file__.rsplit("/", 1)[0] if "/" in __file__ else ".") + "/.."
sys.path.insert(0, ROOT + "/src")
sys.path.insert(0, ROOT + "/src/lib")
sys.path.insert(0, ROOT)

from sim.clock import ScaledClock  # noqa: E402
from sim.device import FakeDaikin  # noqa: E402
from sim.server import DEFAULT_PORT, serve  # noqa: E402
from smartac.board.host import HostBoard  # noqa: E402
from smartac.shell import api, tasks  # noqa: E402
from smartac.shell.app import App  # noqa: E402
from smartac.shell.store import FileStore  # noqa: E402

PORT = 8080
AC_PORT = DEFAULT_PORT + 10  # not the tests' ports, so make test runs alongside
TOKEN = "dev"


async def main(root, speed):
    try:
        os.mkdir(root)
    except OSError:
        pass  # already there
    clock = ScaledClock(speed)
    await serve(FakeDaikin(clock), port=AC_PORT)
    board = HostBoard(clock=clock, speed=speed)
    app = App(board, FileStore(root), "127.0.0.1", AC_PORT, shadow=True)
    web = api.make(app, ROOT + "/web", token=TOKEN)
    print("http://127.0.0.1:%d  token %r, files in %s, %gx speed" % (PORT, TOKEN, root, speed))
    await asyncio.gather(app.control(), tasks.housekeeping(board, app), web.start_server("127.0.0.1", PORT))


args = sys.argv[1:]
if not args:
    print("usage: micropython sim/dev.py <dir> [speed]")
    sys.exit(2)
try:
    asyncio.run(main(args[0], float(args[1]) if len(args) > 1 else 1.0))
except KeyboardInterrupt:
    pass
