#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
# Read-only status of the Daikin AC via its local /dsiot API.
# Field map follows pydaikin's BRP084 (firmware 2.8+/3.x) support.
import json
import os
import sys
import urllib.request

IP = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("DAIKIN_IP")
if not IP:
    sys.exit("usage: daikin_status.py <ac-ip>  (or set DAIKIN_IP)")
BASE = "/dsiot/edge/adr_0100.dgc_status"
OUT = "/dsiot/edge/adr_0200.dgc_status"

MODES = {"0000": "fan", "0100": "heat", "0200": "cool", "0300": "auto", "0500": "dry"}
FANS = {"0A00": "auto", "0B00": "quiet", "0300": "1", "0400": "2",
        "0500": "3", "0600": "4", "0700": "5"}
# per-mode setpoint / fan-speed parameters inside e_3001
SETPOINT = {"cool": "p_02", "heat": "p_03", "auto": "p_1D"}
FANSPEED = {"cool": "p_09", "heat": "p_0A", "auto": "p_26", "dry": "p_27", "fan": "p_28"}


def multireq(paths):
    body = json.dumps({"requests": [{"op": 2, "to": p} for p in paths]}).encode()
    req = urllib.request.Request("http://%s/dsiot/multireq" % IP, body,
                                 {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.load(r)["responses"]


def find(node, *names):
    for name in names:
        node = next(c for c in node["pch"] if c["pn"] == name)
    return node["pv"]


def le_int(hexstr, signed=False):
    return int.from_bytes(bytes.fromhex(hexstr), "little", signed=signed)


indoor, outdoor, power = multireq([BASE, OUT, "/dsiot/edge/adr_0100.i_power.week_power"])
root_in = next(c for c in indoor["pc"]["pch"] if c["pn"] == "e_1002")
root_out = next(c for c in outdoor["pc"]["pch"] if c["pn"] == "e_1003")

on = find(root_in, "e_A002", "p_01") == "01"
mode = MODES.get(find(root_in, "e_3001", "p_01"), "unknown")
room_t = le_int(find(root_in, "e_A00B", "p_01"), signed=True)
room_h = le_int(find(root_in, "e_A00B", "p_02"))
out_t = le_int(find(root_out, "e_A00D", "p_01"), signed=True) / 2

print("Power:        ", "ON" if on else "OFF")
print("Mode:         ", mode)
if mode in SETPOINT:
    print("Target temp:   %.1f °C" % (le_int(find(root_in, "e_3001", SETPOINT[mode])) / 2))
if mode in FANSPEED:
    print("Fan speed:    ", FANS.get(find(root_in, "e_3001", FANSPEED[mode]), "?"))
print("Room temp:     %d °C" % room_t)
print("Room humidity: %d %%" % room_h)
print("Outdoor temp:  %.1f °C" % out_t)
if "pc" in power:  # not answered by the simulator (rsc 4000)
    wp = {c["pn"]: c["pv"] for c in power["pc"]["pch"]}
    print("Today runtime: %s min" % wp.get("today_runtime"))
