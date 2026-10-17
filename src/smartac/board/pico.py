# SPDX-License-Identifier: GPL-3.0-only
# Board for the Pico 2 W (docs/firmware.md, "Tested on the device").
# - machine.reset() reports WDT_RESET on the RP2350, like a real watchdog reset. So reset(reason) first writes
#   reset_reason.txt; the next boot takes its cause from that file and removes it.
# - Wi-Fi power saving is switched off (PM_NONE) on every connect.
# - The largest free block has no API: it comes from micropython.mem_info()'s "max free sz" (16-byte blocks),
#   copied through os.dupterm. About 11 ms; the text also goes to the USB serial.
import gc
import io
import os
import sys
import time

import machine
import micropython
import network

from smartac.board.base import POWER_ON, UNKNOWN, WATCHDOG, Board, wait_event

REASON_FILE = "/reset_reason.txt"
BLINK_MS = 150
GC_BLOCK = 16


class _Capture(io.IOBase):
    # A dupterm stream that keeps what is written to it; reads nothing.
    def __init__(self, size=256):
        self.buf = bytearray(size)
        self.n = 0

    def write(self, b):
        k = min(len(b), len(self.buf) - self.n)
        self.buf[self.n : self.n + k] = b[:k]
        self.n += k
        return len(b)

    def readinto(self, b):
        return None

    def ioctl(self, req, arg):
        return 0


def _take_reason():
    try:
        with open(REASON_FILE) as f:
            reason = f.read().strip()
        os.remove(REASON_FILE)
    except OSError:
        return None
    return reason or None


class PicoBoard(Board):
    def __init__(self, secrets=None):
        # secrets: the secrets module (WIFI_SSID, WIFI_PASSWORD); None means no Wi-Fi.
        self.secrets = secrets
        self._cause = _take_reason() or {machine.PWRON_RESET: POWER_ON, machine.WDT_RESET: WATCHDOG}.get(
            machine.reset_cause(), UNKNOWN
        )
        self.wdt = None
        self.pin = machine.Pin("LED", machine.Pin.OUT)
        self.timer = None
        self.pattern = None
        self.wlan = network.WLAN(network.STA_IF)
        self._capture = _Capture()

    def ticks_ms(self):
        return time.ticks_ms()

    async def wait(self, event, ms):
        return await wait_event(event, ms)

    def wdt_start(self, ms):
        self.wdt = machine.WDT(timeout=ms)

    def wdt_feed(self):
        if self.wdt is not None:
            self.wdt.feed()

    def led(self, pattern):
        if pattern == self.pattern:
            return
        self.pattern = pattern
        if self.timer is not None:
            self.timer.deinit()
            self.timer = None
        if pattern == "error":
            self.timer = machine.Timer(period=BLINK_MS, callback=lambda t: self.pin.toggle())
        else:
            self.pin.value(1 if pattern == "on" else 0)

    def reset(self, reason):
        try:
            with open(REASON_FILE, "w") as f:
                f.write(reason)
        except OSError:
            pass
        time.sleep_ms(100)  # let the USB serial flush
        machine.reset()

    def reset_cause(self):
        return self._cause

    def link_up(self):
        return self.wlan.isconnected()

    def wifi_connect(self):
        if self.secrets is None:
            return
        w = self.wlan
        w.active(True)
        w.config(pm=w.PM_NONE)
        w.connect(self.secrets.WIFI_SSID, self.secrets.WIFI_PASSWORD)

    def wifi_disconnect(self):
        self.wlan.disconnect()

    def rssi(self):
        if not self.wlan.isconnected():
            return None
        try:
            return self.wlan.status("rssi")
        except (OSError, ValueError):
            return None

    def heap(self):
        free = gc.mem_free()
        cap = self._capture
        cap.n = 0
        old = os.dupterm(cap, 0)
        try:
            micropython.mem_info()
        finally:
            os.dupterm(old, 0)
        text = bytes(cap.buf[: cap.n]).decode()
        i = text.find("max free sz:")
        if i < 0:
            return free, None
        try:
            return free, int(text[i + 12 :].split()[0]) * GC_BLOCK
        except (ValueError, IndexError):
            return free, None

    def device_info(self):
        v = sys.implementation.version
        connected = self.wlan.isconnected()
        return {
            "fw": "%d.%d.%d" % (v[0], v[1], v[2]),
            "ip": self.wlan.ifconfig()[0] if connected else None,
            "rssi": self.rssi(),
        }
