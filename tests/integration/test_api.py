# SPDX-License-Identifier: GPL-3.0-only
# The HTTP routes (shell/api.py) on 127.0.0.1: an app that read the simulator once (observe mode, or shadow mode for
# the controller routes), raw requests.
import asyncio
import json
import os
import unittest

import simhttp

from sim.clock import FakeClock
from sim.device import FakeDaikin
from sim.server import DEFAULT_PORT, serve
from smartac.board.host import HostBoard
from smartac.core import types as t
from smartac.shell import api
from smartac.shell.app import MAX_ACTIONS, App
from smartac.shell.store import EVENT_LOG_BYTES, FileStore, MemoryStore

PORT = DEFAULT_PORT + 1
WWW = (__file__.rsplit("/", 1)[0] if "/" in __file__ else ".") + "/../../web"


def get(path):
    return ("GET %s HTTP/1.1\r\nHost: pico\r\n\r\n" % path).encode()


TOKEN = "test-token-123"


def post(path, body, token=None):
    data = body if isinstance(body, bytes) else json.dumps(body).encode()
    head = "POST %s HTTP/1.1\r\nHost: pico\r\nContent-Type: application/json\r\nContent-Length: %d\r\n" % (
        path, len(data))
    if token is not None:
        head += "X-Token: %s\r\n" % token
    return (head + "\r\n").encode() + data


def exchange(*requests, timeout_ms=api.REQUEST_TIMEOUT_MS, shadow=False, token=None, store=None, after=None):
    # Start the app's server, send each raw request on its own connection. -> [(status, body) or None].
    # after(app): awaited once the replies are in, e.g. a tick that applies a queued action.
    async def main():
        clock = FakeClock()
        device = FakeDaikin(clock)
        sim = await serve(device)
        observe = not shadow
        app = App(HostBoard(clock=clock), store or MemoryStore(), "127.0.0.1", DEFAULT_PORT, observe=observe,
                  shadow=shadow)
        await app.tick()
        web = api.make(app, WWW, timeout_ms, token=token)
        task = asyncio.create_task(web.start_server("127.0.0.1", PORT))
        await asyncio.sleep_ms(50)
        try:
            replies = []
            for r in requests:  # no await in a comprehension on MicroPython
                replies.append(await simhttp.raw(PORT, r))
            if after is not None:
                await after(app)
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
        replies = exchange(get("/api/profiles"), get("/www/index.html"), b"POST /api/state HTTP/1.1\r\n\r\n",
                           get("/api/active"))
        self.assertEqual([r[0] for r in replies], [404, 404, 405, 405])
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


def night(**kw):
    return post("/api/active", dict({"profile": "night", "cycle": "finish"}, **kw), TOKEN)


class TestActive(unittest.TestCase):
    def test_token_is_required(self):
        replies = exchange(post("/api/active", {"profile": "night"}), post("/api/active", {"profile": "night"}, "x"),
                           post("/api/active", {"profile": "night"}, TOKEN + "4"), shadow=True, token=TOKEN)
        self.assertEqual([r[0] for r in replies], [401, 401, 401])
        self.assertEqual(json.loads(replies[0][1]), {"error": "token missing or wrong"})

    def test_no_token_configured_refuses_everything(self):
        replies = exchange(night(), post("/api/active", {"profile": "night"}, ""), shadow=True, token=None)
        self.assertEqual([r[0] for r in replies], [401, 401])
        self.assertEqual(json.loads(replies[0][1]), {"error": "no API_TOKEN in secrets.py"})

    def test_bad_bodies(self):
        replies = exchange(
            post("/api/active", {"profile": "nope"}, TOKEN),
            post("/api/active", {"profile": ["night"]}, TOKEN),
            post("/api/active", {"cycle": "finish"}, TOKEN),
            night(cycle="later"),
            post("/api/active", b"{not json", TOKEN),
            post("/api/active", [1], TOKEN),
            shadow=True,
            token=TOKEN,
        )
        self.assertEqual([r[0] for r in replies], [400] * 6)
        self.assertEqual(json.loads(replies[0][1]), {"error": "unknown profile"})
        self.assertEqual(json.loads(replies[3][1]), {"error": "cycle must be finish or leave"})

    def test_observe_mode_has_no_controller(self):
        [(status, body)] = exchange(night(), token=TOKEN)
        self.assertEqual((status, json.loads(body)), (409, {"error": "no controller in observe mode"}))

    def test_queued_then_applied_by_the_next_tick(self):
        store = MemoryStore()

        async def tick(app):
            self.assertEqual(app.queue, [{"action": t.ACT_SET_PROFILE, "profile": "night", "cycle": "finish"},
                                         {"action": t.ACT_SET_PROFILE, "profile": "off", "cycle": "leave"},
                                         {"action": t.ACT_SET_PROFILE, "profile": "night", "cycle": "finish"}])
            await app.tick()

        plain = post("/api/active", {"profile": "night"}, TOKEN)  # cycle defaults to finish
        replies = exchange(night(), night(cycle="leave", profile="off"), plain, shadow=True, token=TOKEN, store=store,
                           after=tick)
        self.assertEqual([r[0] for r in replies], [202, 202, 202])
        s = json.loads(replies[0][1])
        self.assertEqual((s["profile"], s["shadow"]), ("off", True))  # the state from before the tick
        self.assertEqual(store.load("profiles")["active"], "night")
        kinds = [json.loads(line)["kind"] for line in store.events]
        self.assertEqual(kinds.count(t.EV_PROFILE), 3)

    def test_full_queue(self):
        replies = exchange(*[night() for _ in range(MAX_ACTIONS + 1)], shadow=True, token=TOKEN)
        self.assertEqual([r[0] for r in replies], [202] * MAX_ACTIONS + [503])


class TestEvents(unittest.TestCase):
    def test_route(self):
        [(status, body)] = exchange(get("/api/events?limit=5"), shadow=True)
        rows = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual([r["kind"] for r in rows], [t.EV_BOOT])
        self.assertEqual(sorted(rows[0].keys()), ["age_s", "boot", "hum", "kind", "msg", "temp"])

    def test_limit(self):
        app = App(HostBoard(clock=FakeClock()), MemoryStore(), shadow=True)
        for n in range(150):
            app.event(t.EV_LOW_MEMORY, str(n))
        self.assertEqual([r["msg"] for r in app.event_rows("2")], ["149", "148"])
        self.assertEqual([len(app.event_rows(v)) for v in ("0", "x", None, 100, "99999")], [1, 100, 100, 100, 151])


def _tmpdir():
    base = (os.getenv("TMPDIR") or "/tmp").rstrip("/")
    path = "%s/smartac-api-%s" % (base, "".join("%02x" % b for b in os.urandom(4)))
    os.mkdir(path)
    return path


class TestLog(unittest.TestCase):
    def test_across_a_rotation(self):
        root = _tmpdir()
        try:
            store = FileStore(root)
            n = EVENT_LOG_BYTES // 60  # ~75-byte lines: one rotation, nothing dropped yet
            for i in range(n):
                store.append_event(json.dumps({"boot": 0, "up_s": i, "kind": "heap", "msg": "x" * 40}))
            self.assertTrue("events.1.log" in os.listdir(root))
            [(status, body)] = exchange(get("/api/log"), shadow=True, store=store)
            lines = [json.loads(line) for line in body.decode().splitlines()]
            old = [d["up_s"] for d in lines if d["boot"] == 0]
            self.assertEqual(status, 200)
            self.assertEqual(old, list(range(n)))
            self.assertEqual((lines[-1]["boot"], lines[-1]["kind"]), (1, t.EV_BOOT))
        finally:
            for name in os.listdir(root):
                os.remove(root + "/" + name)
            os.rmdir(root)

    def test_earlier_boots_oldest_first(self):
        store = MemoryStore()
        for n in range(3):
            store.append_event(json.dumps({"boot": 0, "up_s": n, "kind": "heap", "msg": "m%d" % n}))
        [(status, body)] = exchange(get("/api/log"), shadow=True, store=store)
        lines = [json.loads(line) for line in body.decode().splitlines()]
        self.assertEqual(status, 200)
        self.assertEqual([(d["boot"], d["kind"]) for d in lines], [(0, "heap")] * 3 + [(1, t.EV_BOOT)])
