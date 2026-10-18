# SPDX-License-Identifier: GPL-3.0-only
# HTTP routes (idea.md, "HTTP API"): GET /, /editor.js, /api/state, /api/history, /api/events, /api/log,
# GET/PUT /api/profiles and /api/settings, POST /api/profiles/validate and /reset, and POST /api/active (brought
# forward from M5 for shadow mode). GETs are open on the LAN; a POST or PUT needs X-Token equal to API_TOKEN from
# secrets.py, and with no token configured every one is refused. Unknown paths -> 404, other methods -> 405.
# Microdot has no read timeout: a client that stops mid-request would keep its socket and task forever, so each
# request runs under a deadline. /api/history and /api/log are streamed.
# Bodies: Microdot reads up to max_body_length itself; a config document (up to 16 KB) is read from the stream only
# after the token check, one at a time (a parsed 16 KB document takes ~90 KB of heap). Larger than
# max_content_length -> 413 before any route runs.
import asyncio
import json
import sys

from microdot import Microdot, Request, Response

from smartac.core.config import OFF
from smartac.core.controller import FINISH, LEAVE
from smartac.core.types import ACT_SET_PROFILE

REQUEST_TIMEOUT_MS = 10000
MAX_DOC_BODY = 17 * 1024  # config.MAX_DOC_BYTES plus room for the page's whitespace
DRAIN_CHUNK = 512
Request.max_content_length = MAX_DOC_BODY
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


def _invalid(msg):
    return {"ok": False, "errors": [{"path": "$", "msg": msg}]}, 400


async def _drain(req):
    # Read and drop a body Microdot left on the stream, so the reply isn't lost to a reset on close.
    left = req.content_length if req.content_length > Request.max_body_length else 0
    while left > 0:
        data = await req.stream.read(min(left, DRAIN_CHUNK))
        if not data:
            break
        left -= len(data)


async def _read_json(req):
    # -> (document, None) or (None, error message).
    try:
        if req.content_length > Request.max_body_length:
            data = await req.stream.readexactly(req.content_length)
        else:
            data = req.body
        return json.loads(data.decode()), None
    except (EOFError, ValueError):  # UnicodeError is a ValueError
        return None, "body must be a JSON document"


def make(app, www="/www", timeout_ms=REQUEST_TIMEOUT_MS, token=None):
    # app: shell.app.App; token: API_TOKEN, None refuses every POST.
    # -> the Microdot server; start it with `await server.start_server(host, port)`.
    web = _Server(timeout_ms)
    saving = [False]  # one config document in memory at a time

    def refused():
        return _error(401, "token missing or wrong" if token else "no API_TOKEN in secrets.py")

    async def with_doc(req, handler):
        # Token, then the body, then handler(document) -> (status, body).
        if not _token_ok(req.headers.get("X-Token"), token):
            await _drain(req)
            return refused()
        if saving[0]:
            await _drain(req)
            return _error(503, "busy with another save, try again")
        saving[0] = True
        try:
            doc, err = await _read_json(req)
            if err is not None:
                return _invalid(err)
            status, body = handler(doc)
            return body, status
        finally:
            saving[0] = False

    @web.get("/")
    async def index(req):
        return Response.send_file(www + "/index.html", max_age=0)

    @web.get("/editor.js")
    async def editor(req):
        return Response.send_file(www + "/editor.js", max_age=0)

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
            return refused()
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

    @web.get("/api/profiles")
    async def get_profiles(req):
        doc = app.profiles_doc()
        return _error(409, "no controller in observe mode") if doc is None else doc

    @web.put("/api/profiles")
    async def put_profiles(req):
        return await with_doc(req, app.save_profiles)

    @web.post("/api/profiles/validate")
    async def validate_profiles(req):
        return await with_doc(req, app.validate_profiles)

    @web.post("/api/profiles/reset")
    async def reset_profiles(req):
        if not _token_ok(req.headers.get("X-Token"), token):
            return refused()
        if saving[0]:
            return _error(503, "busy with another save, try again")
        status, body = app.reset_profiles()
        return body, status

    @web.get("/api/settings")
    async def get_settings(req):
        return app.settings_doc()

    @web.put("/api/settings")
    async def put_settings(req):
        return await with_doc(req, app.save_settings)

    @web.errorhandler(413)
    async def too_large(req):
        return _error(413, "body larger than %d KB" % (MAX_DOC_BODY // 1024))

    @web.errorhandler(404)
    async def not_found(req):
        return {"error": "not found"}, 404

    return web
