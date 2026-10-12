# SPDX-License-Identifier: GPL-3.0-only
# Minimal HTTP/1.1 front for FakeDaikin: one POST /dsiot/multireq per connection, then close.
# No keep-alive, no chunked bodies. An unreachable device closes the connection without answering.
import asyncio
import json

DEFAULT_PORT = 8765  # fixed: MicroPython's Server doesn't expose the socket, so port 0 can't be read back
PATH = "/dsiot/multireq"
MAX_BODY = 8 * 1024
MAX_HEADER_LINES = 50
READ_TIMEOUT_S = 2
DISCARD_MAX = 64 * 1024
DISCARD_IDLE_MS = 50

REASONS = {
    200: "OK",
    400: "Bad Request",
    404: "Not Found",
    405: "Method Not Allowed",
    411: "Length Required",
    413: "Content Too Large",
}


async def _readline(reader):
    return await asyncio.wait_for(reader.readline(), READ_TIMEOUT_S)


async def _request(reader):
    # -> (status, body). status 200 means body is the parsed JSON.
    line = await _readline(reader)
    parts = line.decode().split()
    if len(parts) != 3 or not parts[2].startswith("HTTP/1."):
        return 400, None
    method, target = parts[0], parts[1]
    length = None
    for _ in range(MAX_HEADER_LINES):
        line = await _readline(reader)
        if line in (b"\r\n", b"\n", b""):
            break
        name, sep, value = line.decode().partition(":")
        if sep and name.strip().lower() == "content-length":
            try:
                length = int(value.strip())
            except ValueError:
                return 400, None
    else:
        return 400, None
    if target.split("?")[0] != PATH:
        return 404, None
    if method != "POST":
        return 405, None
    if length is None:
        return 411, None
    if length < 0:
        return 400, None
    if length > MAX_BODY:
        return 413, None
    data = await asyncio.wait_for(reader.readexactly(length), READ_TIMEOUT_S) if length else b""
    try:
        return 200, json.loads(data)
    except ValueError:
        return 400, None


async def _reply(writer, status, payload=None):
    body = b"" if payload is None else json.dumps(payload).encode()
    head = "HTTP/1.1 %d %s\r\n" % (status, REASONS[status])
    if payload is not None:
        head += "Content-Type: application/json\r\n"
    head += "Content-Length: %d\r\nConnection: close\r\n\r\n" % len(body)
    writer.write(head.encode() + body)
    await writer.drain()


async def _discard(reader):
    # Read what the client still sends after an early error reply: closing with unread input resets the
    # connection, and the client can lose the reply.
    for _ in range(DISCARD_MAX // 1024):
        try:
            if not await asyncio.wait_for_ms(reader.read(1024), DISCARD_IDLE_MS):
                return
        except asyncio.TimeoutError:
            return


def _handler(device):
    async def handle(reader, writer):
        try:
            status, body = await _request(reader)
            if status != 200:
                await _reply(writer, status)
                await _discard(reader)
                return
            response = device.handle(body)
            if response is not None:
                await _reply(writer, 200, response)
        except (asyncio.TimeoutError, EOFError, OSError, UnicodeError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()

    return handle


async def serve(device, host="127.0.0.1", port=DEFAULT_PORT):
    # Start serving `device` (FakeDaikin) and return the asyncio Server; close() and wait_closed() stop it.
    return await asyncio.start_server(_handler(device), host, port)
