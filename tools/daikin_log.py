#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
# Read-only logger: one reading of the Daikin AC sensors per interval, appended to a CSV.
# usage: daikin_log.py <ac-ip> [csv-path] [interval-s]   (or set DAIKIN_IP)
import csv
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone

IP = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("DAIKIN_IP")
if not IP:
    sys.exit("usage: daikin_log.py <ac-ip> [csv-path] [interval-s]  (or set DAIKIN_IP)")
CSV = sys.argv[2] if len(sys.argv) > 2 else "local/sensor_log.csv"
INTERVAL = int(sys.argv[3]) if len(sys.argv) > 3 else 60
PATHS = ["/dsiot/edge/adr_0100.dgc_status", "/dsiot/edge/adr_0200.dgc_status"]
FIELDS = ["utc", "power", "mode", "room_t", "room_h", "out_t",
          "raw_in_A00B", "raw_out_A00D", "error"]


def multireq(paths):
    body = json.dumps({"requests": [{"op": 2, "to": p} for p in paths]}).encode()
    req = urllib.request.Request("http://%s/dsiot/multireq" % IP, body,
                                 {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.load(r)["responses"]


def child(node, name):
    return next(c for c in node["pch"] if c["pn"] == name)


def le_int(hexstr, signed=False):
    return int.from_bytes(bytes.fromhex(hexstr), "little", signed=signed)


def reading():
    indoor, outdoor = multireq(PATHS)
    e_in = child(indoor["pc"], "e_1002")
    e_out = child(outdoor["pc"], "e_1003")
    a00b = {c["pn"]: c["pv"] for c in child(e_in, "e_A00B")["pch"]}
    a00d = {c["pn"]: c["pv"] for c in child(e_out, "e_A00D")["pch"]}
    return {
        "power": child(child(e_in, "e_A002"), "p_01")["pv"],
        "mode": child(child(e_in, "e_3001"), "p_01")["pv"],
        "room_t": le_int(a00b["p_01"], signed=True),
        "room_h": le_int(a00b["p_02"]),
        "out_t": le_int(a00d["p_01"], signed=True) / 2,
        # every field of the sensor nodes, to spot ones not decoded yet
        "raw_in_A00B": json.dumps(a00b, separators=(",", ":")),
        "raw_out_A00D": json.dumps(a00d, separators=(",", ":")),
    }


def main():
    new = not os.path.exists(CSV)
    with open(CSV, "a", newline="") as f:
        w = csv.DictWriter(f, FIELDS)
        if new:
            w.writeheader()
        while True:
            row = {"utc": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            try:
                row.update(reading())
            except Exception as e:  # keep logging through AC/Wi-Fi hiccups
                row["error"] = repr(e)
            w.writerow(row)
            f.flush()
            time.sleep(INTERVAL - time.time() % INTERVAL)


if __name__ == "__main__":
    main()
