# SPDX-License-Identifier: GPL-3.0-only
# Tiny HTTP client for the simulator tests: raw bytes and status codes, which the app's client (shell/daikin.py) hides.
import asyncio
import json

HOST = "127.0.0.1"


async def raw(port, data, head=False):
    # Send raw bytes, read until the server closes. -> (status, body bytes), or None if it closed with no reply.
    # head: (status, body, the header block as text) instead.
    reader, writer = await asyncio.open_connection(HOST, port)
    try:
        writer.write(data)
        await writer.drain()
        reply = b""
        while True:
            chunk = await asyncio.wait_for(reader.read(1024), 2)
            if not chunk:
                break
            reply += chunk
    finally:
        writer.close()
        await writer.wait_closed()
    if not reply:
        return None
    top, _, body = reply.partition(b"\r\n\r\n")
    status = int(top.split(b" ")[1])
    return (status, body, top.decode()) if head else (status, body)


def post_bytes(body, path="/dsiot/multireq"):
    head = "POST %s HTTP/1.1\r\nHost: %s\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n"
    return (head % (path, HOST, len(body))).encode() + body


async def multireq(port, payload):
    # POST a JSON body. -> (status, parsed JSON or None), or None if the server closed with no reply.
    reply = await raw(port, post_bytes(json.dumps(payload).encode()))
    if reply is None:
        return None
    status, body = reply
    return status, json.loads(body) if body else None
