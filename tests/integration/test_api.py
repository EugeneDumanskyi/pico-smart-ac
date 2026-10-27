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
from smartac.core import config
from smartac.core import types as t
from smartac.shell import api
from smartac.shell.app import MAX_ACTIONS, App
from smartac.shell.store import EVENT_LOG_BYTES, FileStore, MemoryStore

PORT = DEFAULT_PORT + 1
WWW = (__file__.rsplit("/", 1)[0] if "/" in __file__ else ".") + "/../../web"


def get(path):
    return ("GET %s HTTP/1.1\r\nHost: pico\r\n\r\n" % path).encode()


TOKEN = "test-token-123"


def post(path, body, token=None, method="POST"):
    data = body if isinstance(body, bytes) else json.dumps(body).encode()
    head = "%s %s HTTP/1.1\r\nHost: pico\r\nContent-Type: application/json\r\nContent-Length: %d\r\n" % (
        method, path, len(data))
    if token is not None:
        head += "X-Token: %s\r\n" % token
    return (head + "\r\n").encode() + data


def put(path, body, token=None):
    return post(path, body, token, "PUT")


def exchange(*requests, timeout_ms=api.REQUEST_TIMEOUT_MS, shadow=False, token=None, store=None, before=None,
             after=None, www=WWW):
    # Start the app's server, send each raw request on its own connection. -> [(status, body) or None].
    # A request may also be `async f(port) -> reply`, for exchanges that need more than one connection.
    # before(app): awaited after the first tick, before any request; after(app): awaited once the replies are in,
    # e.g. a tick that applies a queued action.
    async def main():
        clock = FakeClock()
        device = FakeDaikin(clock)
        sim = await serve(device)
        observe = not shadow
        app = App(HostBoard(clock=clock), store or MemoryStore(), "127.0.0.1", DEFAULT_PORT, observe=observe,
                  shadow=shadow)
        await app.tick()
        if before is not None:
            await before(app)
        web = api.make(app, www, timeout_ms, token=token)
        task = asyncio.create_task(web.start_server("127.0.0.1", PORT))
        await asyncio.sleep_ms(50)
        try:
            replies = []
            for r in requests:  # no await in a comprehension on MicroPython
                replies.append(await (simhttp.raw(PORT, r) if isinstance(r, bytes) else r(PORT)))
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

    def test_gzipped_page(self):
        # A built www (tools/build_web.py) has a .gz copy next to each file; a client without gzip gets the plain one.
        root = _tmpdir()
        try:
            for name, data in (("index.html", b"<p>plain</p>"), ("index.html.gz", b"\x1f\x8bpacked"),
                               ("editor.js", b"// plain")):
                with open(root + "/" + name, "wb") as f:
                    f.write(data)

            def page(path, accept=None):
                head = "GET %s HTTP/1.1\r\nHost: pico\r\n" % path
                if accept:
                    head += "Accept-Encoding: %s\r\n" % accept
                return lambda port: simhttp.raw(port, (head + "\r\n").encode(), head=True)

            replies = exchange(page("/", "gzip, deflate, br"), page("/"), page("/", "br"), page("/editor.js", "gzip"),
                               www=root)
        finally:
            for name in os.listdir(root):
                os.remove(root + "/" + name)
            os.rmdir(root)
        self.assertEqual([r[:2] for r in replies], [(200, b"\x1f\x8bpacked"), (200, b"<p>plain</p>"),
                                                    (200, b"<p>plain</p>"), (200, b"// plain")])
        heads = [r[2].lower() for r in replies]
        self.assertIn("content-encoding: gzip", heads[0])
        self.assertIn("content-type: text/html", heads[0])
        self.assertFalse("content-encoding" in heads[1] or "content-encoding" in heads[3])
        self.assertIn("vary: accept-encoding", heads[1])

    def test_unknown_path_and_method(self):
        replies = exchange(get("/api/nope"), get("/www/index.html"), b"POST /api/state HTTP/1.1\r\n\r\n",
                           get("/api/active"))
        self.assertEqual([r[0] for r in replies], [404, 404, 405, 405])
        self.assertEqual(json.loads(replies[0][1]), {"error": "not found"})

    def test_large_body_is_refused(self):
        req = b"PUT /api/profiles HTTP/1.1\r\nContent-Length: %d\r\n\r\n" % (17 * 1024 + 1)
        [(status, body)] = exchange(req)
        self.assertEqual((status, json.loads(body)), (413, {"error": "body larger than 17 KB"}))

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


def act(token=TOKEN, **body):
    return post("/api/action", body, token)


class TestAction(unittest.TestCase):
    def test_token_is_required(self):
        replies = exchange(post("/api/action", {"action": "stop"}), act("x", action="stop"), shadow=True, token=TOKEN)
        self.assertEqual([r[0] for r in replies], [401, 401])
        [(status, body)] = exchange(act(action="stop"), shadow=True, token=None)
        self.assertEqual((status, json.loads(body)), (401, {"error": "no API_TOKEN in secrets.py"}))

    def test_bad_bodies(self):
        bad = [
            ({"action": "set_profile", "profile": "night"}, "unknown action"),
            ({"action": "explode"}, "unknown action"),
            ({}, "unknown action"),
            ({"action": "pause", "minutes": 0}, "minutes must be 1-480 or null"),
            ({"action": "pause", "minutes": 481}, "minutes must be 1-480 or null"),
            ({"action": "pause", "minutes": "60"}, "minutes must be 1-480 or null"),
            ({"action": "pause", "minutes": True}, "minutes must be 1-480 or null"),
            ({"action": "pause", "cycle": "later"}, "cycle must be finish or leave"),
            ({"action": "run", "profile": "nope", "rule": 0}, "unknown profile"),
            ({"action": "run", "profile": "off", "rule": 0}, "unknown profile"),
            ({"action": "run", "profile": "day", "rule": 2}, "rule must be an index in the profile"),
            ({"action": "run", "profile": "day", "rule": -1}, "rule must be an index in the profile"),
            ({"action": "run", "profile": "day"}, "rule must be an index in the profile"),
        ]
        replies = exchange(*[act(**b) for b, _ in bad], post("/api/action", b"[1]", TOKEN), shadow=True, token=TOKEN)
        self.assertEqual([r[0] for r in replies], [400] * (len(bad) + 1))
        self.assertEqual([json.loads(r[1])["error"] for r in replies[:-1]], [m for _, m in bad])
        self.assertEqual(json.loads(replies[-1][1]), {"error": "body must be a JSON object"})

    def test_observe_mode(self):
        [(status, body)] = exchange(act(action="stop"), token=TOKEN)
        self.assertEqual((status, json.loads(body)), (409, {"error": "no controller in observe mode"}))

    def test_queues_exactly_the_action(self):
        async def queued(app):
            self.assertEqual(app.queue, [
                {"action": "pause", "minutes": 60, "cycle": "leave"},
                {"action": "pause", "minutes": None},
                {"action": "run", "profile": "night", "rule": 1},
                {"action": "resume"}, {"action": "stop"}, {"action": "stop_now"}, {"action": "clear_manual"},
            ])

        replies = exchange(act(action="pause", minutes=60, cycle="leave", extra=1), act(action="pause", minutes=None),
                           act(action="run", profile="night", rule=1), act(action="resume"), act(action="stop"),
                           act(action="stop_now"), act(action="clear_manual"), shadow=True, token=TOKEN,
                           after=queued)
        self.assertEqual([r[0] for r in replies], [202] * 7)
        self.assertEqual(json.loads(replies[0][1])["state"], t.IDLE)  # the state from before the tick

    def test_full_queue(self):
        replies = exchange(*[act(action="stop") for _ in range(MAX_ACTIONS + 1)], shadow=True, token=TOKEN)
        self.assertEqual([r[0] for r in replies], [202] * MAX_ACTIONS + [503])
        self.assertEqual(json.loads(replies[-1][1]), {"error": "busy, try again"})


def ac_test(body, token=TOKEN):
    return post("/api/ac/test", body, token)


class TestACTest(unittest.TestCase):
    def test_reachable(self):
        replies = exchange(ac_test({}), ac_test({"ip": "127.0.0.1"}), ac_test({"ip": None}), shadow=True, token=TOKEN)
        self.assertEqual([r[0] for r in replies], [200] * 3)
        for _, body in replies:
            d = json.loads(body)
            self.assertTrue(isinstance(d.pop("ms"), int))
            self.assertEqual(d, {"reachable": True, "fw": "3.6.0", "type": "RA", "comm_err": 0, "ip": "127.0.0.1"})

    def test_refused(self):
        replies = exchange(ac_test({"ip": "ac.local"}), ac_test({"ip": 5}), ac_test({"ip": "192.0.2.300"}),
                           ac_test({}, None), ac_test({}, "x"), shadow=True, token=TOKEN)
        self.assertEqual([r[0] for r in replies], [400, 400, 400, 401, 401])
        self.assertEqual(json.loads(replies[0][1]), {"error": "ip must be an IPv4 address"})

    def test_observe_mode_tests_too(self):
        [(status, body)] = exchange(ac_test({}), token=TOKEN)
        self.assertEqual((status, json.loads(body)["reachable"]), (200, True))

    def test_one_at_a_time(self):
        held = []

        async def hold(app):
            await app.client.lock.acquire()  # a control-task exchange under way
            held.append(app.client.lock)

        async def two(port):
            first = asyncio.create_task(simhttp.raw(port, ac_test({})))
            await asyncio.sleep_ms(50)
            second = await simhttp.raw(port, ac_test({"ip": "127.0.0.1"}))
            held[0].release()
            return second, await first

        [(second, first)] = exchange(two, shadow=True, token=TOKEN, before=hold)
        self.assertEqual((second[0], json.loads(second[1])), (503, {"error": "busy with another test, try again"}))
        self.assertEqual((first[0], json.loads(first[1])["reachable"]), (200, True))


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


    def test_tail(self):
        store = MemoryStore()
        for n in range(600):
            store.append_event(json.dumps({"boot": 0, "up_s": n, "kind": "heap", "msg": "m"}))
        paths = ("/api/log?tail=3", "/api/log?tail=0", "/api/log?tail=9999", "/api/log?tail=x", "/api/log?tail=")
        replies = exchange(*[get(p) for p in paths], shadow=True, store=store)
        lines = [[json.loads(line) for line in body.decode().splitlines()] for _, body in replies]
        self.assertEqual([r[0] for r in replies], [200] * 5)
        self.assertEqual([d["up_s"] for d in lines[0][:2]] + [lines[0][2]["kind"]], [598, 599, t.EV_BOOT])
        self.assertEqual([len(x) for x in lines], [3, 1, 500, 601, 601])  # clamped to 1..500, else the whole log


def doc_body(doc):
    return json.dumps(doc).encode()


def custom_active_store(active="mine"):
    store = MemoryStore()
    doc = config.default_profiles()
    doc["profiles"]["mine"] = doc["profiles"]["day"]
    doc["active"] = active
    store.save("profiles", doc)
    return store


class TestConfigRoutes(unittest.TestCase):
    def test_get_documents(self):
        replies = exchange(get("/api/profiles"), get("/api/settings"), shadow=True)
        self.assertEqual([r[0] for r in replies], [200, 200])
        self.assertEqual(json.loads(replies[0][1]), config.default_profiles())
        self.assertEqual(json.loads(replies[1][1]), config.default_settings("127.0.0.1"))

    def test_token_is_required_and_the_body_is_drained(self):
        big = doc_body(config.default_profiles())  # > max_body_length: left on the stream by Microdot
        self.assertTrue(len(big) > 1024)
        replies = exchange(put("/api/profiles", big), put("/api/profiles", big, "x"),
                           post("/api/profiles/validate", big), post("/api/profiles/reset", {}),
                           put("/api/settings", config.default_settings()), shadow=True, token=TOKEN)
        self.assertEqual([r[0] for r in replies], [401] * 5)
        self.assertEqual(json.loads(replies[0][1]), {"error": "token missing or wrong"})

    def test_save_then_applied_by_the_next_tick(self):
        store = MemoryStore()
        doc = config.default_profiles()
        doc["profiles"]["day"]["label"] = "Daytime"

        async def tick(app):
            self.assertEqual(app.controller.profiles["profiles"]["day"]["label"], "Day")
            await app.tick()
            self.assertEqual(app.controller.profiles["profiles"]["day"]["label"], "Daytime")

        replies = exchange(put("/api/profiles", doc, TOKEN), get("/api/profiles"), shadow=True, token=TOKEN,
                           store=store, after=tick)
        self.assertEqual((replies[0][0], json.loads(replies[0][1])), (200, {"ok": True, "warnings": []}))
        self.assertEqual(json.loads(replies[1][1])["profiles"]["day"]["label"], "Daytime")  # the pending one
        self.assertEqual(store.load("profiles")["profiles"]["day"]["label"], "Daytime")
        self.assertIn(t.EV_CONFIG_SAVED, [json.loads(line)["kind"] for line in store.events])

    def test_errors_carry_paths(self):
        doc = config.default_profiles()
        doc["profiles"]["day"]["rules"][0]["phases"][0]["target_c"] = 31
        doc["profiles"]["night"]["rules"][1]["confirm_reads"] = 0
        store = MemoryStore()
        replies = exchange(put("/api/profiles", doc, TOKEN), put("/api/profiles", b"{nope", TOKEN),
                           put("/api/profiles", b"\xff\xfe", TOKEN), shadow=True, token=TOKEN, store=store)
        self.assertEqual([r[0] for r in replies], [400] * 3)
        body = json.loads(replies[0][1])
        self.assertEqual(body["ok"], False)
        self.assertEqual(sorted(e["path"] for e in body["errors"]),
                         ["profiles.day.rules[0].phases[0].target_c", "profiles.night.rules[1].confirm_reads"])
        self.assertEqual(json.loads(replies[1][1])["errors"], [{"path": "$", "msg": "body must be a JSON document"}])
        self.assertIsNone(store.load("profiles"))

    def test_validate_does_not_save(self):
        doc = config.default_profiles()
        doc["profiles"]["day"]["rules"][0]["phases"].pop()
        store = MemoryStore()

        async def nothing_pending(app):
            self.assertEqual(app.pending, {})

        [(status, body)] = exchange(post("/api/profiles/validate", doc, TOKEN), shadow=True, token=TOKEN, store=store,
                                    after=nothing_pending)
        self.assertEqual(status, 200)
        self.assertEqual([w["path"] for w in json.loads(body)["warnings"]], ["profiles.day.rules[0].phases"])
        self.assertIsNone(store.load("profiles"))

    def test_active_comes_from_the_controller(self):
        doc = config.default_profiles()
        doc["active"] = "day"  # ignored
        store = custom_active_store()
        gone = config.default_profiles()
        replies = exchange(put("/api/profiles", doc, TOKEN), put("/api/profiles", gone, TOKEN), get("/api/profiles"),
                           shadow=True, token=TOKEN, store=store)
        self.assertEqual([r[0] for r in replies], [400, 400, 200])
        self.assertEqual(json.loads(replies[0][1])["errors"], [{"path": "profiles.mine",
                                                               "msg": "the active profile; switch first"}])
        self.assertEqual(json.loads(replies[2][1])["active"], "mine")

    def test_reset_with_a_custom_active_profile_queues_off(self):
        store = custom_active_store()

        async def tick(app):
            await app.tick()
            self.assertEqual(app.controller.active, "off")

        [(status, body)] = exchange(post("/api/profiles/reset", {}, TOKEN), shadow=True, token=TOKEN, store=store,
                                    after=tick)
        self.assertEqual((status, json.loads(body)), (200, {"ok": True, "warnings": []}))
        self.assertEqual(store.load("profiles"), config.default_profiles())

    def test_settings(self):
        store = MemoryStore()
        low_cap = dict(config.default_settings("127.0.0.1"), hard_cap_min=50)
        owner = dict(config.default_settings("127.0.0.1"), dry_run=False, poll_s=90)

        async def tick(app):
            await app.tick()
            self.assertEqual((app.controller.settings["dry_run"], app.poll_ms()), (True, 90 * 1000))

        replies = exchange(put("/api/settings", low_cap, TOKEN), put("/api/settings", owner, TOKEN),
                           get("/api/settings"), shadow=True, token=TOKEN, store=store, after=tick)
        self.assertEqual([r[0] for r in replies], [400, 200, 200])
        self.assertEqual(json.loads(replies[0][1])["errors"][0]["path"], "hard_cap_min")
        self.assertEqual(json.loads(replies[2][1]), owner)  # shadow mode doesn't change the file
        self.assertEqual(store.load("settings"), owner)

    def test_observe_mode(self):
        replies = exchange(get("/api/profiles"), put("/api/profiles", config.default_profiles(), TOKEN),
                           put("/api/settings", config.default_settings("127.0.0.1"), TOKEN), token=TOKEN)
        self.assertEqual([r[0] for r in replies], [409, 409, 200])

    def test_one_save_at_a_time(self):
        body = doc_body(config.default_profiles())
        whole = put("/api/profiles", body, TOKEN)
        head, half = whole[: len(whole) - len(body) // 2], whole[len(whole) - len(body) // 2 :]

        async def two(port):
            # The first save stops halfway through its body; a second one comes in meanwhile.
            reader, writer = await asyncio.open_connection(simhttp.HOST, port)
            writer.write(head)
            await writer.drain()
            await asyncio.sleep_ms(50)
            second = await simhttp.raw(port, put("/api/settings", config.default_settings("127.0.0.1"), TOKEN))
            writer.write(half)
            await writer.drain()
            reply = b""
            while True:
                chunk = await asyncio.wait_for(reader.read(1024), 2)
                if not chunk:
                    break
                reply += chunk
            writer.close()
            await writer.wait_closed()
            return second, int(reply.split(b" ")[1])

        [(second, first)] = exchange(two, shadow=True, token=TOKEN)
        self.assertEqual((second[0], json.loads(second[1]), first),
                         (503, {"error": "busy with another save, try again"}, 200))

    def test_editor_script(self):
        [(status, body)] = exchange(get("/editor.js"))
        self.assertEqual(status, 200)
        self.assertTrue(body.startswith(b"// SPDX"))

    def test_history_script(self):
        [(status, body)] = exchange(get("/history.js"))
        self.assertEqual(status, 200)
        self.assertTrue(body.startswith(b"// SPDX"))
