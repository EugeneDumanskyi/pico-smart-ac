# SPDX-License-Identifier: GPL-3.0-only
# The simulator over real sockets on 127.0.0.1: codec round trips and HTTP errors.
import asyncio
import unittest

import simhttp

from sim.clock import FakeClock
from sim.device import FakeDaikin
from sim.server import DEFAULT_PORT, serve
from smartac.core import daikin_codec as codec
from smartac.core import types as t

COOL = t.ACState(True, t.MODE_COOL, 255, t.FAN_AUTO)


def with_server(device, client):
    # Run client(port) against a server for device; -> what client returned.
    async def main():
        server = await serve(device)
        try:
            return await client(DEFAULT_PORT)
        finally:
            server.close()
            await server.wait_closed()

    return asyncio.run(main())


class TestServer(unittest.TestCase):
    def test_read_write_round_trip(self):
        dev = FakeDaikin(FakeClock())

        async def client(port):
            before = await simhttp.multireq(port, codec.READ_REQUEST)
            wrote = await simhttp.multireq(port, codec.write_request(COOL))
            after = await simhttp.multireq(port, codec.READ_REQUEST)
            return before, wrote, after

        before, wrote, after = with_server(dev, client)
        self.assertEqual(before[0], 200)
        self.assertEqual(codec.decode(before[1]).ac, t.ACState(False, t.MODE_FAN, None, "3"))
        self.assertEqual(wrote[0], 200)
        self.assertIsNone(codec.write_result(wrote[1]))
        r = codec.decode(after[1])
        self.assertEqual(r.ac, COOL)
        self.assertEqual((r.temp, r.hum, r.outdoor_temp), (290, 80, 320))

    def test_rejected_write_is_still_http_200(self):
        dev = FakeDaikin(FakeClock())
        dev.reject_writes(1)
        status, body = with_server(dev, lambda port: simhttp.multireq(port, codec.write_request(COOL)))
        self.assertEqual((status, codec.write_result(body)), (200, "rsc 4000"))

    def test_unreachable_closes_without_reply(self):
        clock = FakeClock()
        dev = FakeDaikin(clock)
        dev.unreachable(5)

        async def client(port):
            down = await simhttp.multireq(port, codec.READ_REQUEST)
            clock.advance_min(5)
            up = await simhttp.multireq(port, codec.READ_REQUEST)
            return down, up

        down, up = with_server(dev, client)
        self.assertIsNone(down)
        self.assertEqual(up[0], 200)

    def test_http_errors(self):
        long_body = b"{" + b" " * (8 * 1024) + b"}"
        cases = (
            (b"GET /dsiot/multireq HTTP/1.1\r\nHost: x\r\n\r\n", 405),
            (simhttp.post_bytes(b"{}", "/other"), 404),
            (b"POST /dsiot/multireq HTTP/1.1\r\nHost: x\r\n\r\n{}", 411),
            (b"POST /dsiot/multireq HTTP/1.1\r\nContent-Length: x\r\n\r\n", 400),
            (b"POST /dsiot/multireq HTTP/1.1\r\nContent-Length: -1\r\n\r\n", 400),
            (simhttp.post_bytes(long_body), 413),
            (simhttp.post_bytes(b"{not json"), 400),
            (b"nonsense\r\n\r\n", 400),
        )

        async def client(port):
            replies = []
            for data, _ in cases:
                replies.append(await simhttp.raw(port, data))
            return replies

        replies = with_server(FakeDaikin(FakeClock()), client)
        self.assertEqual([r[0] for r in replies], [want for _, want in cases])
        self.assertEqual({r[1] for r in replies}, {b""})

    def test_query_string_on_path_is_ignored(self):
        reply = with_server(
            FakeDaikin(FakeClock()),
            lambda port: simhttp.raw(port, simhttp.post_bytes(b'{"requests":[]}', "/dsiot/multireq?x=1")),
        )
        self.assertEqual(reply[0], 200)
