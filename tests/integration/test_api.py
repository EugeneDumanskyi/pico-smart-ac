# SPDX-License-Identifier: GPL-3.0-only
# The HTTP routes (shell/api.py) on 127.0.0.1: an observe-mode app that read the simulator once, raw GETs.
import asyncio
import json
import unittest

import simhttp

from sim.clock import FakeClock
from sim.device import FakeDaikin
from sim.server import DEFAULT_PORT, serve
from smartac.board.host import HostBoard
from smartac.shell import api
from smartac.shell.app import App
from smartac.shell.store import MemoryStore

PORT = DEFAULT_PORT + 1
WWW = (__file__.rsplit("/", 1)[0] if "/" in __file__ else ".") + "/../../web"


def get(path):
    return ("GET %s HTTP/1.1\r\nHost: pico\r\n\r\n" % path).encode()


def exchange(*requests, timeout_ms=api.REQUEST_TIMEOUT_MS):
    # Start the app's server, send each raw request on its own connection. -> [(status, body) or None].
    async def main():
        clock = FakeClock()
        device = FakeDaikin(clock)
        sim = await serve(device)
        app = App(HostBoard(clock=clock), MemoryStore(), "127.0.0.1", DEFAULT_PORT, observe=True)
        await app.tick()
        web = api.make(app, WWW, timeout_ms)
        task = asyncio.create_task(web.start_server("127.0.0.1", PORT))
        await asyncio.sleep_ms(50)
        try:
            replies = []
            for r in requests:  # no await in a comprehension on MicroPython
                replies.append(await simhttp.raw(PORT, r))
            return replies
        finally:
            web.shutdown()
            await task
            sim.close()
            await sim.wait_closed()
            assert device.writes == 0

    return asyncio.run(main())


class TestRoutes(unittest.TestCase):
    def test_state(self):
        [(status, body)] = exchange(get("/api/state"))
        s = json.loads(body)
        self.assertEqual((status, s["state"], s["ac"]["reachable"], s["room"]["temp"]), (200, "OBSERVE", True, 29.0))

    def test_history_is_a_day_by_default(self):
        [(status, body)] = exchange(get("/api/history"))
        d = json.loads(body)
        self.assertEqual(status, 200)
        for name in ("temp", "hum", "out", "mode", "power"):
            self.assertEqual(len(d[name]), 1440)
        self.assertEqual((d["temp"][-1], d["power"][-1], d["step_s"]), (29.0, False, 60))

    def test_history_minutes_are_clamped(self):
        replies = exchange(*[get("/api/history?minutes=%s" % m) for m in ("10", "0", "99999", "x")])
        self.assertEqual([len(json.loads(b)["temp"]) for _, b in replies], [10, 1, 1440, 1440])

    def test_page(self):
        [(status, body)] = exchange(get("/"))
        self.assertEqual(status, 200)
        self.assertTrue(b"/api/state" in body)

    def test_unknown_path_and_method(self):
        replies = exchange(get("/api/profiles"), get("/www/index.html"), b"POST /api/state HTTP/1.1\r\n\r\n")
        self.assertEqual([r[0] for r in replies], [404, 404, 405])
        self.assertEqual(json.loads(replies[0][1]), {"error": "not found"})

    def test_large_body_is_refused(self):
        req = b"POST /api/state HTTP/1.1\r\nContent-Length: 5000\r\n\r\n"
        [(status, _)] = exchange(req)
        self.assertEqual(status, 413)

    def test_stalled_request_is_closed(self):
        # Headers never finish: the deadline closes the connection without a reply, and the server goes on.
        replies = exchange(b"GET /api/state HTTP/1.1\r\n", get("/api/state"), timeout_ms=200)
        self.assertIsNone(replies[0])
        self.assertEqual(replies[1][0], 200)
