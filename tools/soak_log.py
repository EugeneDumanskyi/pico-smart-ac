#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
# Soak logger on the Mac: GET /api/state from the Pico every minute, one CSV row each, into local/ (gitignored).
#   python3 tools/soak_log.py <pico-ip> [interval_s]      (or PICO_IP in the environment)
# The M1 verdict is read from this file: no unexplained reset (boot and resets), heap_free and heap_max_block flat
# after the first hour, Wi-Fi drops recovered. A failed poll is a row with an error, so gaps show too.
import csv
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIELDS = [
    "utc", "error", "uptime_s", "boot", "reset_cause", "resets", "heap_free", "heap_max_block", "heap_min_free",
    "heap_min_block", "low_memory", "rssi", "wifi_drops", "wifi_reconnects", "ac_reconnects", "reachable",
    "unreachable_s", "reads", "read_errors", "writes", "temp", "hum", "last_crash",
]


def row(ip):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        with urllib.request.urlopen("http://%s/api/state" % ip, timeout=10) as r:
            s = json.load(r)
    except (OSError, ValueError) as e:
        return {"utc": now, "error": str(e)[:80]}
    d = s.get("device", {})
    out = {"utc": now, "error": ""}
    for k in FIELDS[2:]:
        out[k] = d.get(k, s.get(k))
    out["resets"] = json.dumps(d.get("resets"), sort_keys=True)
    out["reachable"] = s["ac"]["reachable"]
    out["temp"] = s["room"]["temp"]
    out["hum"] = s["room"]["hum"]
    return out


def main(argv):
    ip = argv[1] if len(argv) > 1 else os.environ.get("PICO_IP")
    if not ip:
        sys.exit("usage: soak_log.py <pico-ip> [interval_s]")
    interval = float(argv[2]) if len(argv) > 2 else 60
    path = ROOT / "local" / ("soak-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%SZ") + ".csv")
    path.parent.mkdir(exist_ok=True)
    print("logging to %s every %g s, Ctrl-C stops" % (path.relative_to(ROOT), interval))
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, FIELDS)
        w.writeheader()
        while True:
            start = time.monotonic()
            r = row(ip)
            w.writerow(r)
            f.flush()
            if r["error"]:
                print(r["utc"], r["error"])
            else:
                print(r["utc"], "up %(uptime_s)s s, boot %(boot)s, heap %(heap_free)s / %(heap_max_block)s" % r)
            time.sleep(max(0.0, interval - (time.monotonic() - start)))


if __name__ == "__main__":
    try:
        main(sys.argv)
    except KeyboardInterrupt:
        pass
