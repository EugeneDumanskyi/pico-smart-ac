# SPDX-License-Identifier: GPL-3.0-only
# Async client for the adapter's /dsiot/multireq (docs/DAIKIN_API.md). One request at a time, one connection per
# request (Connection: close), the whole exchange under a hard timeout. read() and write() never raise: any
# failure is an unreachable Reading or an error string, and the core decides what it means.
# The AC is reached by IPv4 literal only: a name would go to DNS, which blocks the loop (architecture.md).
import asyncio
import json

from smartac.core import daikin_codec as codec
from smartac.core.config import is_ipv4
from smartac.core.types import Reading

PATH = "/dsiot/multireq"
MAX_HEAD = 4 * 1024
MAX_HEADER_LINES = 32
MAX_BODY = 16 * 1024
CHUNK = 512


class HTTPError(Exception):
    pass


def _parse_head(head):
    # Status line and headers -> (status, content length or None).
    lines = head.split(b"\r\n")
    parts = lines[0].split(None, 2)
    if len(parts) < 2 or not parts[0].startswith(b"HTTP/1."):
        raise HTTPError("bad status line")
    status = int(parts[1])
    if len(lines) - 1 > MAX_HEADER_LINES:
        raise HTTPError("too many headers")
    length = None
    for line in lines[1:]:
        name, sep, value = line.partition(b":")
        if not sep:
            raise HTTPError("bad header")
        name = name.strip().lower()
        if name == b"content-length":
            length = int(value.strip())
            if length < 0:
                raise HTTPError("bad content-length")
        elif name == b"transfer-encoding":
            raise HTTPError("chunked response")
    return status, length


class DaikinClient:
    def __init__(self, ip, port=80, timeout_ms=3000, limits=None):
        # limits: settings sensor_limits, passed to the codec.
        if not is_ipv4(ip):
            raise ValueError("AC address must be an IPv4 address")
        self.ip = ip
        self.port = port
        self.timeout_ms = timeout_ms
        self.limits = limits
        self.lock = asyncio.Lock()

    async def read(self):
        doc, err = await self._post(codec.READ_REQUEST)
        if err is not None:
            return Reading.unreachable(err)
        return codec.decode(doc, self.limits)

    async def write(self, state):
        # ACState -> None if the adapter accepted it, else why. Whether it applied is for the read-back to say.
        try:
            body = codec.write_request(state)
        except ValueError as e:
            return "not written: %s" % e
        doc, err = await self._post(body)
        if err is not None:
            return err
        return codec.write_result(doc)

    async def _post(self, payload):
        # -> (parsed JSON, None) or (None, why).
        data = json.dumps(payload).encode()
        conn = []
        async with self.lock:
            try:
                return await asyncio.wait_for_ms(self._exchange(data, conn), self.timeout_ms), None
            except asyncio.TimeoutError:
                return None, "timeout"
            except OSError as e:
                return None, "network error %s" % (e.errno if hasattr(e, "errno") else e)
            except EOFError:
                return None, "no reply"
            except HTTPError as e:
                return None, str(e)
            except (ValueError, UnicodeError):
                return None, "bad reply"
            finally:
                for writer in conn:
                    try:
                        writer.close()
                        await writer.wait_closed()
                    except OSError:
                        pass

    async def _exchange(self, data, conn):
        reader, writer = await asyncio.open_connection(self.ip, self.port)
        conn.append(writer)
        head = "POST %s HTTP/1.1\r\nHost: %s\r\nContent-Type: application/json\r\n" % (PATH, self.ip)
        head += "Content-Length: %d\r\nConnection: close\r\n\r\n" % len(data)
        writer.write(head.encode() + data)
        await writer.drain()

        buf = b""
        while True:
            end = buf.find(b"\r\n\r\n")
            if end >= 0:
                break
            if len(buf) > MAX_HEAD:
                raise HTTPError("headers too long")
            chunk = await reader.read(CHUNK)
            if not chunk:
                raise EOFError
            buf += chunk
        status, length = _parse_head(buf[:end])
        body = buf[end + 4 :]
        if status != 200:
            raise HTTPError("http %d" % status)
        if length is not None and length > MAX_BODY:
            raise HTTPError("reply too large")
        while length is None or len(body) < length:
            chunk = await reader.read(CHUNK)
            if not chunk:
                if length is not None:
                    raise EOFError
                break
            body += chunk
            if len(body) > MAX_BODY:
                raise HTTPError("reply too large")
        if length is not None:
            body = body[:length]
        return json.loads(body)
