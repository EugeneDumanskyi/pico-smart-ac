# SPDX-License-Identifier: GPL-3.0-only
# Device test: pulling the power while profiles.json is being saved leaves a valid document. No AC access.
# In maintenance mode (tools/deploy.py --stay):  mpremote run tests/device/save_check.py
# Each run checks what the last pull left, then saves profiles.json over and over (the first profile's label counts
# up) until you pull the USB cable; plug it back in and run the script again. After ROUNDS pulls it puts the
# original document back and stops. The original is kept in /save_check.json until then.
import json
import os

from smartac.core import config
from smartac.shell.store import FileStore

MARKER = "/save_check.json"
ROUNDS = 5


def exists(path):
    try:
        os.stat(path)
        return True
    except OSError:
        return False


def source():
    # Which file load() would take: the document itself, or the .bak after a cut between the renames.
    try:
        with open("/profiles.json") as f:
            json.loads(f.read())
        return "profiles.json"
    except (OSError, ValueError):
        return "profiles.json.bak"


store = FileStore("/")

if exists(MARKER):
    with open(MARKER) as f:
        mark = json.loads(f.read())
    doc = store.load("profiles")
    errors = config.validate_profiles(doc)[0] if doc is not None else [{"path": "$", "msg": "nothing to load"}]
    first = sorted(doc["profiles"])[0] if doc is not None and not errors else None
    label = doc["profiles"][first].get("label") if first else None
    print("round %d: loaded %s, label %r, errors %r" % (mark["round"], source(), label, errors))
    print("PASS" if not errors else "FAIL: the next boot would use the defaults")
    mark["round"] += 1
else:
    original = store.load("profiles") or config.default_profiles()
    mark = {"round": 1, "original": original}

if mark["round"] > ROUNDS:
    store.save("profiles", mark["original"])
    os.remove(MARKER)
    print("done: the original profiles.json is back")
else:
    with open(MARKER, "w") as f:
        f.write(json.dumps(mark))
    doc = mark["original"]
    first = sorted(doc["profiles"])[0]
    print("saving profiles.json in a loop: pull the USB cable now, then run this script again")
    n = 0
    while True:
        n += 1
        doc["profiles"][first]["label"] = "check %d" % n
        store.save("profiles", doc)
        if n % 20 == 0:
            print(n)
