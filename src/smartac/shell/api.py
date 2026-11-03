# SPDX-License-Identifier: GPL-3.0-only
# HTTP routes (idea.md, "HTTP API"): GET /, /editor.js, /history.js,
# /apple-touch-icon.png, /api/state, /api/history, /api/events,
# /api/log[?tail=N], GET/PUT /api/profiles and /api/settings, POST /api/profiles/validate and /reset, POST /api/active,
# /api/action, /api/clock and /api/ac/test. GETs are open on the LAN; a POST or PUT needs X-Token equal to API_TOKEN
# from secrets.py, and with no token configured every one is refused. Unknown paths -> 404, other methods -> 405.
# Microdot has no read timeout: a client that stops mid-request would keep its socket and task forever, so each
# request runs under a deadline. /api/history and /api/log are streamed. The page's files go out gzipped
# (tools/build_web.py) to a client that takes gzip, streamed from flash as they are.
# Bodies: Microdot reads up to max_body_length itself; a config document (up to 16 KB) is read from the stream only
# after the token check, one at a time (a parsed 16 KB document takes ~90 KB of heap). Larger than
# max_content_length -> 413 before any route runs.
import asyncio
import gc
import json
import os
import sys

from microdot import Microdot, Request, Response

from smartac.core.config import OFF, is_ipv4
from smartac.core.controller import FINISH, LEAVE, MAX_PAUSE_MIN
from smartac.core.types import (
    ACT_CLEAR_MANUAL,
    ACT_PAUSE,
    ACT_RESUME,
    ACT_RUN,
    ACT_SET_PROFILE,
    ACT_STOP,
    ACT_STOP_NOW,
)

REQUEST_TIMEOUT_MS = 10000
ACTIONS = (ACT_PAUSE, ACT_RESUME, ACT_RUN, ACT_STOP, ACT_STOP_NOW, ACT_CLEAR_MANUAL)  # set_profile: /api/active
MAX_DOC_BODY = 17 * 1024  # config.MAX_DOC_BYTES plus room for the page's whitespace
DRAIN_CHUNK = 512
MAX_TAIL = 500  # /api/log?tail=
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
        # The body and the reply (a few KB each) go now, before the next tick's small objects settle between them
        # and split the heap: a soak saw the largest block fall below LOW_BLOCK after an evening of profile edits.
        gc.collect()


def _asset(req, path):
    # The .gz copy when the client takes gzip and it exists, else the plain file (make dev serves web/ unbuilt).
    gz = "gzip" in req.headers.get("Accept-Encoding", "")
    if gz:
        try:
            os.stat(path + ".gz")
        except OSError:
            gz = False
    resp = Response.send_file(path, max_age=0, compressed=gz, file_extension=".gz" if gz else "")
    resp.headers["Vary"] = "Accept-Encoding"
    return resp


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


def _int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _tail(v):
    # ?tail= -> 1..MAX_TAIL, or None (the whole log) for a missing or garbage value.
    try:
        return min(max(int(v), 1), MAX_TAIL)
    except (TypeError, ValueError):
        return None


def _check_action(body, profiles):
    # The shape of an /api/action body -> an error message or None. Whether it applies in the current state is for
    # the core to say (an `ignored` event).
    name = body.get("action")
    if name == ACT_PAUSE:
        minutes = body.get("minutes")
        if minutes is not None and (not _int(minutes) or not 1 <= minutes <= MAX_PAUSE_MIN):
            return "minutes must be 1-%d or null" % MAX_PAUSE_MIN
        if body.get("cycle", FINISH) not in (FINISH, LEAVE):
            return "cycle must be finish or leave"
    elif name == ACT_RUN:
        profile = profiles.get(body.get("profile")) if isinstance(body.get("profile"), str) else None
        if not isinstance(profile, dict):
            return "unknown profile"
        i = body.get("rule")
        if not _int(i) or not 0 <= i < len(profile.get("rules", [])):
            return "rule must be an index in the profile"
    elif name not in ACTIONS:
        return "unknown action"
    return None


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
        return json.loads(data), None  # bytes as they are: a decoded copy is one more large block
    except (EOFError, ValueError):  # UnicodeError is a ValueError
        return None, "body must be a JSON document"


def make(app, www="/www", timeout_ms=REQUEST_TIMEOUT_MS, token=None):
    # app: shell.app.App; token: API_TOKEN, None refuses every POST.
    # -> the Microdot server; start it with `await server.start_server(host, port)`.
    web = _Server(timeout_ms)
    saving = [False]  # one config document in memory at a time
    testing = [False]  # one AC test at a time

    def refused():
        return _error(401, "token missing or wrong" if token else "no API_TOKEN in secrets.py")

    def json_object(req):
        # Token, then a small JSON object body. -> (body, None) or (None, error reply).
        if not _token_ok(req.headers.get("X-Token"), token):
            return None, refused()
        try:
            body = req.json
        except ValueError:
            body = None
        if not isinstance(body, dict):
            return None, _error(400, "body must be a JSON object")
        return body, None

    def queue(action):
        if not app.action(action):
            return _error(503, "busy, try again")
        return app.status(), 202

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
        return _asset(req, www + "/index.html")

    @web.get("/editor.js")
    async def editor(req):
        return _asset(req, www + "/editor.js")

    @web.get("/history.js")
    async def history_js(req):
        return _asset(req, www + "/history.js")

    @web.get("/apple-touch-icon.png")  # iOS asks for this path itself; the page links it too
    async def icon(req):
        return _asset(req, www + "/apple-touch-icon.png")

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
        # The flash log as NDJSON, oldest first, earlier boots included (tools/shadow_pull.py). ?tail=N: the last N
        # lines only (the History tab).
        tail = _tail(req.args.get("tail"))
        body = app.store.read_events() if tail is None else app.store.tail_events(tail)
        headers = {"Content-Type": "application/x-ndjson", "Cache-Control": "no-store"}
        return Response(body=body, headers=headers)

    @web.post("/api/active")
    async def active(req):
        # {"profile": "night", "cycle": "finish" | "leave"} -> 202 and the state; the next tick applies it.
        body, err = json_object(req)
        if err is not None:
            return err
        ctl = app.controller
        if ctl is None:
            return _error(409, "no controller in observe mode")
        name = body.get("profile")
        if not isinstance(name, str) or (name != OFF and name not in ctl.profiles.get("profiles", {})):
            return _error(400, "unknown profile")
        cycle = body.get("cycle", FINISH)
        if cycle not in (FINISH, LEAVE):
            return _error(400, "cycle must be finish or leave")
        return queue({"action": ACT_SET_PROFILE, "profile": name, "cycle": cycle})

    @web.post("/api/action")
    async def action(req):
        # {"action": "pause", "minutes": 60 | null, "cycle": "finish" | "leave"}, {"action": "run", "profile": "day",
        # "rule": 0}, or resume, stop, stop_now, clear_manual -> 202 and the state; the next tick applies it, and one
        # that doesn't apply in that state becomes an `ignored` event.
        body, err = json_object(req)
        if err is not None:
            return err
        ctl = app.controller
        if ctl is None:
            return _error(409, "no controller in observe mode")
        msg = _check_action(body, ctl.profiles.get("profiles", {}))
        if msg is not None:
            return _error(400, msg)
        keys = ("action", "minutes", "cycle", "profile", "rule")
        return queue({k: body[k] for k in keys if k in body})

    @web.post("/api/clock")
    async def phone_clock(req):
        # {"time": "2026-10-28T09:32:02Z"}: the page's local time (ADR 0006) -> 200 {"time", "source", "phone_s"}.
        body, err = json_object(req)
        if err is not None:
            return err
        status, reply = app.phone_time(body.get("time"))
        return reply, status

    @web.post("/api/ac/test")
    async def ac_test(req):
        # {"ip": "192.0.2.10"} (optional: the address in use) -> 200 {"reachable": true, "fw", "type", "comm_err",
        # "ip", "ms"} or {"reachable": false, "error", "ip"}. Read-only; one test at a time.
        body, err = json_object(req)
        if err is not None:
            return err
        ip = body.get("ip")
        if ip is not None and (not isinstance(ip, str) or not is_ipv4(ip)):
            return _error(400, "ip must be an IPv4 address")
        if testing[0]:
            return _error(503, "busy with another test, try again")
        testing[0] = True
        try:
            return await app.test_ac(ip)
        finally:
            testing[0] = False

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
