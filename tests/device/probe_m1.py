# SPDX-License-Identifier: GPL-3.0-only
# M1 step 0: what the observer design depends on (docs/firmware.md, "Tested on the device"). Read only, no AC.
# Needs secrets.py on the board, and Microdot as .mpy in :probe_mpy/microdot (mpy-cross each src/lib/microdot/*.py,
# then  mpremote mkdir :probe_mpy; mpremote cp -r <dir> :probe_mpy/). Without it, check 4 imports :lib/microdot.
# Run twice:  mpremote run tests/device/probe_m1.py
# The first run measures, leaves probe_reset.txt and calls machine.reset(); the second prints the reset cause.
# Check 4 must be the first import in a fresh interpreter: the qstrs a first import interns stay on the heap.
import gc
import io
import os
import sys
import time

import machine
import micropython

MARKER = "probe_reset.txt"


def exists(path):
    try:
        os.stat(path)
        return True
    except OSError:
        return False


def causes():
    names = {}
    for name in dir(machine):
        if name.endswith("_RESET"):
            names[getattr(machine, name)] = name
    return names


def after_reset():
    cause = machine.reset_cause()
    with open(MARKER) as f:
        before = f.read()
    os.remove(MARKER)
    print("1. reset_cause() after machine.reset(): %d %s (before the reset: %s)" % (cause, causes().get(cause), before))


def max_block():
    # Largest allocatable bytearray, by binary search; a failed attempt runs a collection first.
    lo, hi = 0, gc.mem_free() + 1
    while hi - lo > 16:
        mid = (lo + hi) // 2
        try:
            b = bytearray(mid)
            del b
            lo = mid
        except MemoryError:
            hi = mid
    return lo


def probe_heap():
    gc.collect()
    print("2. heap free %d" % gc.mem_free())
    print("   API for the largest block:", [n for n in dir(gc) + dir(sys) if "block" in n or "frag" in n])
    for _ in range(3):
        gc.collect()
        t = time.ticks_us()
        n = max_block()
        print("   bytearray probe: max block %d in %d us" % (n, time.ticks_diff(time.ticks_us(), t)))
    # Fragment the heap a little and probe again.
    junk = [bytearray(1000) for _ in range(100)]
    for i in range(0, len(junk), 2):
        junk[i] = None
    gc.collect()
    t = time.ticks_us()
    n = max_block()
    print("   fragmented: free %d, max block %d in %d us" % (gc.mem_free(), n, time.ticks_diff(time.ticks_us(), t)))
    t = time.ticks_us()
    info = mem_info()
    print("   mem_info via dupterm in %d us: %r" % (time.ticks_diff(time.ticks_us(), t), info))
    del junk


class _Capture(io.IOBase):
    def __init__(self):
        self.buf = bytearray(256)
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


def mem_info():
    # micropython.mem_info() also goes to the terminal; dupterm slot 0 gets a copy. "max free sz" is in 16-byte blocks.
    cap = _Capture()
    old = os.dupterm(cap, 0)
    try:
        micropython.mem_info()
    finally:
        os.dupterm(old, 0)
    text = bytes(cap.buf[: cap.n]).decode()
    i = text.find("max free sz:")
    return text[i:].strip() if i >= 0 else text


def probe_wifi():
    import network
    import secrets

    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    print("3. pm before: %#x; PM_NONE=%#x" % (wlan.config("pm"), wlan.PM_NONE))
    try:
        wlan.config(pm=wlan.PM_NONE)
        print("   pm=PM_NONE accepted, pm now %#x" % wlan.config("pm"))
    except Exception as e:
        print("   pm=PM_NONE refused: %r" % e)
    t = time.ticks_ms()
    if not wlan.isconnected():
        wlan.connect(secrets.WIFI_SSID, secrets.WIFI_PASSWORD)
        while not wlan.isconnected() and time.ticks_diff(time.ticks_ms(), t) < 15000:
            time.sleep_ms(100)
    print("   connected %s after %d ms, status %d" % (wlan.isconnected(), time.ticks_diff(time.ticks_ms(), t),
                                                     wlan.status()))
    print("   status('rssi') = %r, pm after connect %#x" % (wlan.status("rssi"), wlan.config("pm")))


def probe_import():
    path = "/probe_mpy" if exists("probe_mpy/microdot/microdot.mpy") else "/lib"
    sys.path.insert(0, path)
    gc.collect()
    free = gc.mem_free()
    t = time.ticks_ms()
    import microdot

    took = time.ticks_diff(time.ticks_ms(), t)
    allocated = free - gc.mem_free()
    gc.collect()
    print("4. import %s: %d ms, allocated %d, held %d" % (microdot.__file__, took, allocated, free - gc.mem_free()))
    sys.path.pop(0)


def probe_rename():
    # FileStore relies on os.rename replacing an existing file.
    with open("probe_a.txt", "w") as f:
        f.write("new")
    with open("probe_b.txt", "w") as f:
        f.write("old")
    try:
        os.rename("probe_a.txt", "probe_b.txt")
        with open("probe_b.txt") as f:
            print("5. os.rename onto an existing file: ok, target now %r" % f.read())
    except OSError as e:
        print("5. os.rename onto an existing file: OSError %r" % e)
    for p in ("probe_a.txt", "probe_b.txt"):
        if exists(p):
            os.remove(p)


if exists(MARKER):
    after_reset()
else:
    print("boot cause %d %s; constants %r" % (machine.reset_cause(), causes().get(machine.reset_cause()), causes()))
    probe_import()
    probe_heap()
    probe_wifi()
    probe_rename()
    with open(MARKER, "w") as f:
        f.write("%d" % machine.reset_cause())
    print("1. calling machine.reset(); run this script again for the cause")
    time.sleep_ms(200)
    machine.reset()
