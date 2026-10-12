# SPDX-License-Identifier: GPL-3.0-only
# Golden traces (ADR 0005): each tests/golden/*.json is a config plus a list of controller steps and the exact
# commands each step must return. They are the behaviour contract; review a change to them like a spec change.
#
#   {"name", "profiles": "default" | <profiles.json>, "active", "settings": {overrides of the defaults},
#    "start_ms": clock at boot (negative to cross the ticks wrap), "snapshot": <state.json> | null,
#    "steps": [step, ...]}
#   step: {"t_s": seconds after start_ms, "ac": [power, mode, target °C | null, fan] | null (unreachable),
#          "temp": °C, "hum": %, "readback": bool, "actions": [...], "reboot": bool,
#          "configure": {"profiles": <profiles.json> | "default", "settings": {overrides}},
#          "to_s": repeat the step every "every_s" (default 60) up to this time,
#          "expect": {"state": ..., "cmds": [["write", [p, m, t °C, f]], ["event", kind], ["persist"]]}}
#
# ac, temp and hum carry over from the previous step when left out. "reboot" starts a new controller from the
# last persisted snapshot (state.json survives a restart, RAM doesn't). "configure" saves new config documents
# before the step (Controller.configure): settings overrides are merged into the current settings, a new profiles
# document keeps the current "active", and a later "reboot" uses the new documents. "cmds" defaults to [], so
# every step is checked. Event messages aren't pinned. A snapshot given in the file is in the persisted form
# (tenths of °C).
import json
import os
import time
import unittest

from smartac.core import config
from smartac.core import types as t
from smartac.core.controller import Controller

GOLDEN = __file__.rsplit("/", 1)[0] if "/" in __file__ else "."
TRACES = sorted(name for name in os.listdir(GOLDEN) if name.endswith(".json"))


def _ac(v):
    if v is None:
        return None
    return t.ACState(v[0], v[1], None if v[2] is None else t.tenths(v[2]), v[3])


def _expected(cmds):
    out = []
    for cmd in cmds:
        if cmd[0] == "write":
            out.append(("write", _ac(cmd[1])))
        elif cmd[0] == "event":
            out.append(("event", cmd[1]))
        elif cmd[0] == "persist":
            out.append(("persist",))
        else:
            raise ValueError("unknown command %r" % (cmd[0],))
    return out


def _actual(cmds):
    out = []
    for kind, payload in cmds:
        if kind == t.CMD_WRITE:
            out.append(("write", payload))
        elif kind == t.CMD_EVENT:
            out.append(("event", payload[0]))
        else:
            json.dumps(payload)  # state.json must be able to hold it
            out.append(("persist",))
    return out


def _configure(conf, profiles, settings):
    if "profiles" in conf:
        new = config.default_profiles() if conf["profiles"] == "default" else json.loads(json.dumps(conf["profiles"]))
        new["active"] = profiles["active"]
        profiles = new
    settings = dict(settings)
    settings.update(conf.get("settings", {}))
    return profiles, settings


def run_trace(tc, doc):
    profiles = config.default_profiles() if doc.get("profiles", "default") == "default" else doc["profiles"]
    if "active" in doc:
        profiles["active"] = doc["active"]
    settings = config.default_settings()
    settings.update(doc.get("settings", {}))
    start = time.ticks_add(0, doc.get("start_ms", 0))
    ctl = Controller(profiles, settings, start, doc.get("snapshot"))
    snap = None
    ac = temp = hum = None
    for i, step in enumerate(doc["steps"]):
        if "ac" in step:
            ac = _ac(step["ac"])
        temp = step.get("temp", temp)
        hum = step.get("hum", hum)
        actions = step.get("actions", [])
        if "to_s" in step and actions:
            raise ValueError("step %d: actions can't repeat" % i)
        times = range(step["t_s"], step.get("to_s", step["t_s"]) + 1, step.get("every_s", 60))
        if "configure" in step:
            profiles, settings = _configure(step["configure"], profiles, settings)
            ctl.configure(profiles, settings)
        if step.get("reboot"):
            if snap is not None:
                profiles["active"] = snap["profile"]  # the shell keeps profiles.json "active" in step
            ctl = Controller(profiles, settings, time.ticks_add(start, step["t_s"] * 1000), snap)
        expect = step.get("expect", {})
        want = _expected(expect.get("cmds", []))
        for t_s in times:
            now = time.ticks_add(start, t_s * 1000)
            if ac is None:
                reading = t.Reading.unreachable("trace")
            else:
                reading = t.Reading(ac, temp=None if temp is None else t.tenths(temp), hum=hum)
            cmds = ctl.step(now, reading, actions, step.get("readback", False))
            for kind, payload in cmds:
                if kind == t.CMD_PERSIST:
                    snap = json.loads(json.dumps(payload))
            got = _actual(cmds)
            where = "%s, step %d at t=%ds" % (doc["name"], i, t_s)
            if got != want:
                tc.fail("%s: commands %r, expected %r" % (where, got, want))
            if "state" in expect and ctl.state != expect["state"]:
                tc.fail("%s: state %s, expected %s" % (where, ctl.state, expect["state"]))


class TestGolden(unittest.TestCase):
    def test_traces_present(self):
        self.assertTrue(len(TRACES) >= 20, "golden traces missing: %r" % TRACES)


def _make(path):
    def test(self):
        with open(path) as f:
            run_trace(self, json.load(f))

    return test


for _name in TRACES:
    setattr(TestGolden, "test_" + _name[:-5], _make(GOLDEN + "/" + _name))
