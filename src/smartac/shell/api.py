# SPDX-License-Identifier: GPL-3.0-only
# HTTP routes (idea.md, "HTTP API"): GET /, /api/state, /api/history, /api/events, /api/log, and POST /api/active
# (brought forward from M5 for shadow mode). GETs are open on the LAN; a POST needs X-Token equal to API_TOKEN from
# secrets.py, and with no token configured every POST is refused. Unknown paths -> 404, other methods -> 405.
# Microdot has no read timeout: a client that stops mid-request would keep its socket and task forever, so each
# request runs under a deadline. Request sizes are capped low; /api/history and /api/log are streamed.
import asyncio
import sys

from microdot import Microdot, Request, Response

from smartac.core.config import OFF
from smartac.core.controller import FINISH, LEAVE
from smartac.core.types import ACT_SET_PROFILE

REQUEST_TIMEOUT_MS = 10000
Request.max_content_length = 1024
Request.max_body_length = 1024
Request.max_readline = 1024


class _Server(Microdot):
    def __init__(self, timeout_ms):
        super().__init__()
        self.timeout_ms = timeout_ms

    async def handle_request(self, reader, writer):
        try:
            await asyncio.wait_for_ms(super().handle_request(reader, writer), self.timeout_ms)
        except asyncio.TimeoutError:
            pass
        except Exception as e:  # one bad request must not reach the loop's handler
            sys.print_exception(e)
        try:
            writer.close()
            await writer.wait_closed()
        except OSError:
            pass


def _token_ok(given, token):
    # Compares every character, so the time taken doesn't tell how much of a guess was right.
    if not token or not isinstance(given, str) or len(given) != len(token):
        return False
    diff = 0
    for a, b in zip(given, token):
        diff |= ord(a) ^ ord(b)
    return diff == 0


def _error(status, msg):
    return {"error": msg}, status


def make(app, www="/www", timeout_ms=REQUEST_TIMEOUT_MS, token=None):
    # app: shell.app.App; token: API_TOKEN, None refuses every POST.
    # -> the Microdot server; start it with `await server.start_server(host, port)`.
    web = _Server(timeout_ms)

    @web.get("/")
    async def index(req):
        return Response.send_file(www + "/index.html", max_age=0)

    @web.get("/api/state")
    async def state(req):
        return app.status()

    @web.get("/api/history")
    async def history(req):
        body = app.history_chunks(req.args.get("minutes"))
        return Response(body=body, headers={"Content-Type": "application/json", "Cache-Control": "no-store"})

    @web.get("/api/events")
    async def events(req):
        return app.event_rows(req.args.get("limit", 100))

    @web.get("/api/log")
    async def log(req):
        # The flash log as NDJSON, oldest first, earlier boots included (tools/shadow_pull.py).
        headers = {"Content-Type": "application/x-ndjson", "Cache-Control": "no-store"}
        return Response(body=app.store.read_events(), headers=headers)

    @web.post("/api/active")
    async def active(req):
        # {"profile": "night", "cycle": "finish" | "leave"} -> 202 and the state; the next tick applies it.
        if not _token_ok(req.headers.get("X-Token"), token):
            return _error(401, "token missing or wrong" if token else "no API_TOKEN in secrets.py")
        try:
            body = req.json
        except ValueError:
            body = None
        if not isinstance(body, dict):
            return _error(400, "body must be a JSON object")
        ctl = app.controller
        if ctl is None:
            return _error(409, "no controller in observe mode")
        name = body.get("profile")
        if not isinstance(name, str) or (name != OFF and name not in ctl.profiles.get("profiles", {})):
            return _error(400, "unknown profile")
        cycle = body.get("cycle", FINISH)
        if cycle not in (FINISH, LEAVE):
            return _error(400, "cycle must be finish or leave")
        if not app.action({"action": ACT_SET_PROFILE, "profile": name, "cycle": cycle}):
            return _error(503, "busy, try again")
        return app.status(), 202

    @web.errorhandler(404)
    async def not_found(req):
        return {"error": "not found"}, 404

    return web
