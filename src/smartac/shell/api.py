# SPDX-License-Identifier: GPL-3.0-only
# HTTP routes (idea.md, "HTTP API"), the read-only part for M1: GET /, /api/state, /api/history.
# No POST routes yet, so no token check (GETs are open on the LAN). Unknown paths -> 404, other methods -> 405.
# Microdot has no read timeout: a client that stops mid-request would keep its socket and task forever, so each
# request runs under a deadline. Request sizes are capped low; /api/history is streamed from History.chunks().
import asyncio
import sys

from microdot import Microdot, Request, Response

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


def make(app, www="/www", timeout_ms=REQUEST_TIMEOUT_MS):
    # app: shell.app.App. -> the Microdot server; start it with `await server.start_server(host, port)`.
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

    @web.errorhandler(404)
    async def not_found(req):
        return {"error": "not found"}, 404

    return web
