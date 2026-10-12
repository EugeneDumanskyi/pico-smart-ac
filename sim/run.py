# SPDX-License-Identifier: GPL-3.0-only
# Standalone simulator for manual pokes: micropython sim/run.py [port] [speed]
# Then e.g. python3 tools/daikin_status.py 127.0.0.1:8765. Ctrl-C stops it.
import asyncio
import sys

ROOT = (__file__.rsplit("/", 1)[0] if "/" in __file__ else ".") + "/.."
sys.path.insert(0, ROOT + "/src")
sys.path.insert(0, ROOT)

from sim.clock import MIN_MS, ScaledClock  # noqa: E402
from sim.device import FakeDaikin  # noqa: E402
from sim.server import DEFAULT_PORT, serve  # noqa: E402


async def main(port, speed):
    clock = ScaledClock(speed)
    device = FakeDaikin(clock)
    await serve(device, port=port)
    print("fake Daikin on 127.0.0.1:%d, %gx speed" % (port, speed))
    while True:
        await asyncio.sleep_ms(max(1, int(10 * MIN_MS / speed)))  # report every 10 simulated minutes
        device.sync()
        r = device.room
        print("%s  room %.1f C %.0f %%  coil %.1f" % (device.ac(), r.temp, r.hum, r.coil))


args = sys.argv[1:]
try:
    asyncio.run(main(int(args[0]) if args else DEFAULT_PORT, float(args[1]) if len(args) > 1 else 1.0))
except KeyboardInterrupt:
    pass
