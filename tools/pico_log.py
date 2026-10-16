# SPDX-License-Identifier: GPL-3.0-only
# Read-only logger on the Pico 2 W: one reading of the AC sensors per minute, appended to a CSV on flash.
# For overnight runs on a USB charger (spike S1). Needs secrets.py on the board (WIFI_SSID, WIFI_PASSWORD, DAIKIN_IP).
# Install:  mpremote cp tools/pico_log.py :main.py      (starts on every power-up)
# Fetch:    mpremote cp :sensor_log.csv local/pico_sensor_log.csv
# Stop:     mpremote rm :main.py   (Ctrl-C in the REPL stops a running logger)
# Time is UTC from NTP (one DNS lookup at start-up and every 6 h, a tool-only shortcut; the app has no NTP).
# The LED blinks once per good reading.
import gc
import json
import secrets
import time

import machine
import network
import ntptime
import requests

CSV = "sensor_log.csv"
INTERVAL = 60
URL = "http://%s/dsiot/multireq" % secrets.DAIKIN_IP
QUERY = {"requests": [
    {"op": 2, "to": "/dsiot/edge/adr_0100.dgc_status?filter=pv"},
    {"op": 2, "to": "/dsiot/edge/adr_0200.dgc_status?filter=pv"},
]}
HEADER = "utc,power,mode,room_t,room_h,room_t2,out_t,raw_in_A00B,error\n"
NTP_EVERY = 6 * 3600
MAX_ERRORS = 15  # consecutive failed readings before a reset

led = machine.Pin("LED", machine.Pin.OUT)
wlan = network.WLAN(network.STA_IF)


def wifi():
    if wlan.isconnected():
        return
    wlan.active(True)
    wlan.connect(secrets.WIFI_SSID, secrets.WIFI_PASSWORD)
    for _ in range(30):
        if wlan.isconnected():
            return
        time.sleep(1)
    raise OSError("wifi status %d" % wlan.status())


def child(node, name):
    for c in node["pch"]:
        if c["pn"] == name:
            return c
    raise KeyError(name)


def le_int(hexstr, signed=False):
    v = int.from_bytes(bytes.fromhex(hexstr), "little")
    if signed and v >= 1 << (4 * len(hexstr) - 1):
        v -= 1 << (4 * len(hexstr))
    return v


def reading():
    r = requests.post(URL, json=QUERY, timeout=8)
    try:
        if r.status_code != 200:
            raise OSError("HTTP %d" % r.status_code)
        indoor, outdoor = r.json()["responses"]
    finally:
        r.close()
    e_in = child(indoor["pc"], "e_1002")
    a00b = {c["pn"]: c["pv"] for c in child(e_in, "e_A00B")["pch"]}
    out = child(child(child(outdoor["pc"], "e_1003"), "e_A00D"), "p_01")["pv"]
    return [
        child(child(e_in, "e_A002"), "p_01")["pv"],
        child(child(e_in, "e_3001"), "p_01")["pv"],
        str(le_int(a00b["p_01"], True)),
        str(le_int(a00b["p_02"])),
        str(le_int(a00b["p_05"], True) / 2) if "p_05" in a00b else "",
        str(le_int(out, True) / 2),
        '"%s"' % json.dumps(a00b).replace('"', '""'),
        "",
    ]


def utc():
    t = time.gmtime()
    return "%04d-%02d-%02dT%02d:%02d:%02d+00:00" % t[:6]


def append(line):
    with open(CSV, "a") as f:
        f.write(line)


def main():
    try:
        open(CSV).close()
    except OSError:
        append(HEADER)
    synced = None
    errors = 0
    while True:
        row = ["", "", "", "", "", "", "", ""]
        try:
            wifi()
            if synced is None or time.time() - synced > NTP_EVERY:
                ntptime.settime()
                synced = time.time()
            row = reading()
            errors = 0
            led.on()
        except Exception as e:  # keep logging through AC/Wi-Fi hiccups
            errors += 1
            row[-1] = repr(e).replace(",", ";")
        if synced is not None:  # rows before the first NTP sync would carry a 2021 date
            append(utc() + "," + ",".join(row) + "\n")
        led.off()
        if errors >= MAX_ERRORS:
            machine.reset()
        gc.collect()
        time.sleep(INTERVAL - time.time() % INTERVAL)


main()
