# SPDX-License-Identifier: GPL-3.0-only
# DaikinClient against the simulator and against misbehaving servers on 127.0.0.1.
import asyncio
import unittest

from sim.clock import FakeClock
from sim.device import FakeDaikin
from sim.server import DEFAULT_PORT, serve
from smartac.core import types as t
from smartac.shell.daikin import DaikinClient

HOST = "127.0.0.1"
OTHER_PORT = DEFAULT_PORT + 1  # canned replies
CLOSED_PORT = DEFAULT_PORT + 2  # nothing listens here
COOL = t.ACState(True, t.MODE_COOL, 255, t.FAN_AUTO)


def with_sim(device, client):
    async def main():
        server = await serve(device)
        try:
            return await client(DaikinClient(HOST, DEFAULT_PORT))
        finally:
            server.close()
            await server.wait_closed()

    return asyncio.run(main())


def canned(reply, delay_ms=0, log=None):
    # A server that reads the request, waits, sends `reply` (bytes, or None for nothing), then closes.
    async def handle(reader, writer):
        if log is not None:
            log.append(1)
        try:
            length = 0
            while True:
                line = await reader.readline()
                if line in (b"\r\n", b""):
                    break
                if line.lower().startswith(b"content-length:"):
                    length = int(line.split(b":")[1])
            await reader.readexactly(length)
            await asyncio.sleep_ms(delay_ms)
            if reply is not None:
                writer.write(reply)
                await writer.drain()
        finally:
            if log is not None:
                log.append(-1)
            writer.close()
            await writer.wait_closed()

    return handle


def with_canned(handle, client, timeout_ms=3000):
    async def main():
        server = await asyncio.start_server(handle, HOST, OTHER_PORT)
        try:
            return await client(DaikinClient(HOST, OTHER_PORT, timeout_ms=timeout_ms))
        finally:
            server.close()
            await server.wait_closed()

    return asyncio.run(main())


def reply(body, status="200 OK", length=True):
    head = "HTTP/1.1 %s\r\nContent-Type: application/json\r\n" % status
    if length:
        head += "Content-Length: %d\r\n" % len(body)
    return head.encode() + b"Connection: close\r\n\r\n" + body


class TestSim(unittest.TestCase):
    def test_read(self):
        r = with_sim(FakeDaikin(FakeClock()), lambda c: c.read())
        self.assertEqual(r.ac, t.ACState(False, t.MODE_FAN, None, "3"))
        self.assertEqual((r.temp, r.hum, r.outdoor_temp, r.error), (290, 80, 320, None))

    def test_write_accepted(self):
        dev = FakeDaikin(FakeClock())
        self.assertIsNone(with_sim(dev, lambda c: c.write(COOL)))
        self.assertEqual(dev.ac(), COOL)

    def test_write_rejected(self):
        dev = FakeDaikin(FakeClock())
        dev.reject_writes(1)
        self.assertEqual(with_sim(dev, lambda c: c.write(COOL)), "rsc 4000")

    def test_state_we_never_write(self):
        heat = t.ACState(True, t.MODE_HEAT, 220, t.FAN_AUTO)
        dev = FakeDaikin(FakeClock())
        self.assertTrue(with_sim(dev, lambda c: c.write(heat)).startswith("not written"))
        self.assertEqual(dev.writes, 0)

    def test_outage_is_unreachable(self):
        dev = FakeDaikin(FakeClock())
        dev.unreachable(5)

        async def client(c):
            return await c.read(), await c.write(COOL)

        r, err = with_sim(dev, client)
        self.assertIsNone(r.ac)
        self.assertEqual(r.error, "no reply")
        self.assertEqual(err, "no reply")

    def test_concurrent_requests_take_turns(self):
        log = []

        async def client(c):
            return await asyncio.gather(c.read(), c.read(), c.write(COOL))

        body = b'{"responses":[{"fr":"x","rsc":2004}]}'
        results = with_canned(canned(reply(body), delay_ms=20, log=log), client)
        self.assertEqual(log, [1, -1] * 3)
        self.assertEqual([r.error for r in results[:2]], ["rsc 2004"] * 2)
        self.assertIsNone(results[2])


class TestFailures(unittest.TestCase):
    def read_with(self, data):
        return with_canned(canned(data), lambda c: c.read())

    def test_connection_refused(self):
        r = asyncio.run(DaikinClient(HOST, CLOSED_PORT).read())
        self.assertIsNone(r.ac)
        self.assertTrue(r.error.startswith("network error"), r.error)

    def test_silent_server_times_out(self):
        r = with_canned(canned(None, delay_ms=1000), lambda c: c.read(), timeout_ms=100)
        self.assertEqual(r.error, "timeout")

    def test_bad_json(self):
        self.assertEqual(self.read_with(reply(b"{not json")).error, "bad reply")

    def test_http_error(self):
        self.assertEqual(self.read_with(reply(b"", "404 Not Found")).error, "http 404")

    def test_oversize(self):
        big = b'{"x":"' + b"a" * (17 * 1024) + b'"}'
        self.assertEqual(self.read_with(reply(big)).error, "reply too large")
        self.assertEqual(self.read_with(reply(big, length=False)).error, "reply too large")

    def test_body_to_eof_without_length(self):
        body = b'{"responses":[{"fr":"x","rsc":2004}]}'
        err = with_canned(canned(reply(body, length=False)), lambda c: c.write(COOL))
        self.assertIsNone(err)

    def test_short_body(self):
        head = b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n{}"
        self.assertEqual(self.read_with(head).error, "no reply")

    def test_garbage(self):
        self.assertEqual(self.read_with(b"nonsense\r\n\r\n").error, "bad status line")
        many = b"HTTP/1.1 200 OK\r\n" + b"X: y\r\n" * 40 + b"\r\n{}"
        self.assertEqual(self.read_with(many).error, "too many headers")
        chunked = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n2\r\n{}\r\n0\r\n\r\n"
        self.assertEqual(self.read_with(chunked).error, "chunked response")

    def test_hostname_refused(self):
        for ip in ("daikin.local", "localhost", "", None, "192.0.2", "192.0.2.300"):
            with self.assertRaises(ValueError):
                DaikinClient(ip)
