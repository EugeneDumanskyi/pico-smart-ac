#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
# Shadow-mode review (M3) on the Mac: pull the Pico's flash event log and the 24 h history into local/ (gitignored).
#   python3 tools/shadow_pull.py <pico-ip> [--report]     (or PICO_IP in the environment)
#   python3 tools/shadow_pull.py --report                 (no address: report from the saved pulls only)
# Each pull saves /api/state, /api/log and /api/history raw in local/shadow/<utc-stamp>/. --report merges every pull
# (events deduplicated by boot, up_s, kind and message) and prints one line per decision: local clock time, the
# reading it was made on, the AC state, and the room from 10 min before to 30 min after (from the saved histories).
# The Pico has no clock: an event's time is the pull time minus (uptime_s - up_s), for the boot that was running at a
# pull. Events of a boot no pull saw running show the boot and uptime only.
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PULLS = ROOT / "local" / "shadow"
STAMP = "%Y%m%d-%H%M%SZ"
DECISIONS = ("boot", "profile", "cycle_start", "phase", "would", "hard_cap", "cycle_end", "manual", "takeover",
             "unreachable", "write_failed", "blocked", "config_saved")
WINDOW = (-10, 0, 10, 20, 30)  # minutes around a decision, for the room column


def fetch(ip, path):
    with urllib.request.urlopen("http://%s%s" % (ip, path), timeout=30) as r:
        return r.read()


def pull(ip):
    while True:  # one pull per second at most: the stamp names the directory
        out = PULLS / datetime.now(timezone.utc).strftime(STAMP)
        if not out.exists() and not out.with_name(out.name + ".part").exists():
            break
        time.sleep(1)
    part = out.with_name(out.name + ".part")  # renamed once complete, so a failed pull leaves nothing to merge
    part.mkdir(parents=True)
    meta = {}
    for name, path in (("state.json", "/api/state"), ("log.ndjson", "/api/log"),
                       ("history.json", "/api/history?minutes=1440")):
        body = fetch(ip, path)
        meta[name] = time.time()  # when this reply came in: the ages in it count back from here
        (part / name).write_bytes(body)
    (part / "meta.json").write_text(json.dumps({"pico": ip, "fetched": meta}, indent=1))
    part.rename(out)
    state = json.loads((out / "state.json").read_text())
    d = state["device"]
    lines = (out / "log.ndjson").read_bytes().count(b"\n")
    print("saved %s: boot %d, up %s, %d log lines, writes %d" % (out.relative_to(ROOT), d["boot"],
                                                                  hms(d["uptime_s"]), lines, state["writes"]))
    return out


def hms(s):
    return "%d:%02d:%02d" % (s // 3600, s // 60 % 60, s % 60)


def local(ts):
    return datetime.fromtimestamp(ts).astimezone().strftime("%a %d %b %H:%M")


def load_pulls():
    pulls = []
    for p in sorted(PULLS.glob("*Z")) if PULLS.exists() else ():
        try:
            meta = json.loads((p / "meta.json").read_text())
            state = json.loads((p / "state.json").read_text())
            hist = json.loads((p / "history.json").read_text())
            log = (p / "log.ndjson").read_text()
        except (OSError, ValueError) as e:
            print("skipping %s: %s" % (p.name, e))
            continue
        pulls.append({"dir": p, "meta": meta["fetched"], "state": state, "history": hist, "log": log})
    return pulls


def merge(pulls):
    # -> events (sorted), boot start times {boot: unix s}, room samples {unix minute: (temp, hum, mode, power)}.
    boots = {}
    seen = {}
    room = {}
    for p in pulls:
        d = p["state"]["device"]
        boots.setdefault(d["boot"], p["meta"]["state.json"] - d["uptime_s"])
        for line in p["log"].splitlines():
            try:
                e = json.loads(line)
                key = (e["boot"], e["up_s"], e["kind"], e["msg"])
            except (ValueError, KeyError, TypeError):
                continue  # a line cut by a rotation while streaming
            seen.setdefault(key, e)
        h = p["history"]
        n = len(h["temp"])
        if h["last_age_s"] is None:
            continue
        end = p["meta"]["history.json"] - h["last_age_s"]
        for i in range(n):
            if h["power"][i] is None:
                continue
            minute = int((end - (n - 1 - i) * h["step_s"]) // 60)
            room[minute] = (h["temp"][i], h["hum"][i], h["mode"][i], h["power"][i])
    events = sorted(seen.values(), key=lambda e: (e["boot"], e["up_s"]))
    return events, boots, room


def ac_text(sample):
    if sample is None:
        return "?"
    _, _, mode, power = sample
    return "%s %s" % ("on" if power else "off", mode)


def window(room, ts):
    minute = int(ts // 60)
    cells = []
    for m in WINDOW:
        s = room.get(minute + m)
        cells.append("-" if s is None or s[0] is None else "%.1f/%s" % (s[0], "-" if s[1] is None else s[1]))
    return " ".join(cells)


def num(v, fmt):
    return "-" if v is None else fmt % v


def report(pulls):
    events, boots, room = merge(pulls)
    if not events:
        print("no events in %s" % PULLS.relative_to(ROOT))
        return
    print("%d pulls, %d events, boots %s" % (len(pulls), len(events), ", ".join(str(b) for b in sorted(boots))))
    print("room column: temp/hum at %s min" % " ".join("%+d" % m for m in WINDOW))
    print()
    heap = []
    for e in events:
        if e["kind"] == "heap":
            heap.append(e)
            continue
        if e["kind"] not in DECISIONS:
            continue
        start = boots.get(e["boot"])
        if start is None:
            when = "boot %d +%s" % (e["boot"], hms(e["up_s"]))
            ac = cells = ""
        else:
            ts = start + e["up_s"]
            when = local(ts)
            ac = ac_text(room.get(int(ts // 60)))
            cells = window(room, ts)
        print("%-16s %-12s %-50s %5s %4s  %-9s %s" % (when, e["kind"], e["msg"][:50], num(e.get("temp"), "%.1f"),
                                                    num(e.get("hum"), "%d"), ac, cells))
    last = pulls[-1]["state"]
    d = last["device"]
    print()
    print("soak: boot %d up %s, resets %s, last crash %s" % (d["boot"], hms(d["uptime_s"]), d.get("resets"),
                                                             d.get("last_crash")))
    print("writes %d, write_errors %d, blocked %d, low_memory %s" % (
        last["writes"], last["write_errors"], sum(1 for e in events if e["kind"] == "blocked"), d.get("low_memory")))
    if heap:
        free = [int(e["msg"].split()[1]) for e in heap]
        print("heap: %d hourly lines, free %d..%d, last: %s" % (len(heap), min(free), max(free), heap[-1]["msg"]))


def main(argv):
    args = [a for a in argv[1:] if not a.startswith("--")]
    flags = [a for a in argv[1:] if a.startswith("--")]
    if any(f != "--report" for f in flags):
        sys.exit("usage: shadow_pull.py [pico-ip] [--report]")
    ip = args[0] if args else os.environ.get("PICO_IP")
    if not ip and not flags:
        sys.exit("usage: shadow_pull.py <pico-ip> [--report]   (or PICO_IP)")
    if ip:
        try:
            pull(ip)
        except OSError as e:
            sys.exit("pull from %s failed: %s" % (ip, e))
    if flags:
        report(load_pulls())


if __name__ == "__main__":
    main(sys.argv)
