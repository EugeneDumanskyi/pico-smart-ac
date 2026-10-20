# SPDX-License-Identifier: GPL-3.0-only
# M4 host half: the app writing to the real AC from the Mac, every write approved at the terminal.
#   micropython sim/supervised.py <dir> [--minute-s N] [--sim]       (make supervised)
# The AC address comes from DAIKIN_IP in the environment (no default); --sim runs against FakeDaikin instead.
# --minute-s shrinks the controller's minute (controller.MIN_MS), so every minute timer runs N s; poll_s, SETTLE_MS and
# the AC stay real. The session config is set in real time and converted, so our writes stay GAP_S (4 min) apart.
# Files go to <dir> (FileStore); the same <dir> again is a restart mid-cycle. session.log there holds the start and
# end (UTC) and each approval, so the M3 review can skip these windows.
# A write prints the tick's reading and the state, then waits for y at the prompt. On y the AC is read again, and a
# change since the tick means no write (ADR 0003: decide from a fresh read). Anything else declines it, which the
# core sees as a rejected write.
# HTTP on 127.0.0.1:8080 as in dev.py (token "dev"), plus runner-only routes, for curl from a second terminal:
#   POST /run/action {"action": "run", "profile": "m4", "rule": 0}    any action the core takes (App.action)
#   POST /run/fault {"read": "unreachable" | "ok"}                    reads fail until "ok"
#   POST /run/sim {"remote": {"power": true, "mode": "cool", "target_c": 24, "fan": "auto"}} or {"unreachable": min}
#                                                                     --sim only: a remote press, or the fake AC
#                                                                     silent for that many (real) minutes
# --sim keeps the fake AC's state in <dir>/sim_ac.json across restarts. --port N moves the HTTP port (and --sim's AC
# port with it), so rehearsals can run side by side.
# Ctrl-C reads the AC; if it is on, it offers an approved off write. The default leaves it as it is.
import asyncio
import json
import os
import sys
import time

ROOT = (__file__.rsplit("/", 1)[0] if "/" in __file__ else ".") + "/.."
sys.path.insert(0, ROOT + "/src")
sys.path.insert(0, ROOT + "/src/lib")
sys.path.insert(0, ROOT)

from sim.device import FakeDaikin  # noqa: E402
from sim.server import DEFAULT_PORT, serve  # noqa: E402
from smartac.board.host import HostBoard  # noqa: E402
from smartac.core import config, controller  # noqa: E402
from smartac.core import daikin_codec as codec  # noqa: E402
from smartac.core.events import SHADOW_FLASH_KINDS  # noqa: E402
from smartac.core.types import EV_IGNORED, EV_PAUSE, EV_RESUME, ACState, Reading, tenths, ticks_since  # noqa: E402
from smartac.shell import api, tasks  # noqa: E402
from smartac.shell.app import SETTLE_MS, App  # noqa: E402
from smartac.shell.daikin import DaikinClient  # noqa: E402
from smartac.shell.store import FileStore  # noqa: E402

PORT = 8080
AC_PORT = DEFAULT_PORT + 11  # --sim: not dev.py's or the tests' ports
TOKEN = "dev"
PROFILE = "m4"
MINUTE_S = 15
GAP_S = 4 * 60  # the least time between two of our writes, and between a remote press and our takeover
FLASH_KINDS = SHADOW_FLASH_KINDS + (EV_PAUSE, EV_RESUME, EV_IGNORED)  # every step, for the checklist


def scale_minute(seconds):
    # Every minute timer in the controller runs `seconds` instead. -> the old MIN_MS, to put back.
    old = controller.MIN_MS
    controller.MIN_MS = int(seconds * 1000)
    return old


def _phase(mode, fan, minutes, target_c=None):
    p = {"mode": mode, "fan": fan, "max_min": minutes, "until": None}
    if target_c is not None:
        p["target_c"] = target_c
    return p


def _min(real_s, minute_s, low=1):
    # real_s as controller minutes at minute_s each, rounded up, at least `low` (the validation's minimum).
    return max(low, int(-(-real_s // minute_s)))


def _rule(name, op, phases, cooldown_min, min_interval_min):
    return {
        "name": name,
        "when": {"match": "all", "conditions": [{"sensor": "temp", "op": op, "value": 10}]},
        "confirm_reads": 1,
        "phases": phases,
        "cooldown_min": cooldown_min,
        "min_interval_min": min_interval_min,
    }


def session_profiles(minute_s=MINUTE_S):
    # Times are real, converted to controller minutes; at the default 15 s minute, in controller minutes:
    # Rule 0 triggers on any reading, at most once a real hour (240), so the AC isn't asked again during a session:
    # cool 16 -> fan 16, cooldown 16. Every phase and the cooldown last GAP_S, so our writes are that far apart.
    # Rule 1 never triggers (temp < 10 is below the sensor limits): `run` it for the hard cap. Its phases, cool 24 ->
    # fan 24, add up to the cap (48); only a cool phase that ends late (a fault across its end) lets the cap cut the
    # fan short, and a delay of up to 2 real min still leaves the fan GAP_S.
    m = lambda real_s: _min(real_s, minute_s)  # noqa: E731
    fan_s = GAP_S + 120
    cap = hard_cap(minute_s)
    return {
        "version": config.VERSION,
        "active": PROFILE,
        "profiles": {
            PROFILE: {
                "label": "M4 supervised",
                "rules": [
                    _rule("trigger", ">", [_phase("cool", "auto", m(GAP_S), 26), _phase("fan", "3", m(GAP_S))],
                          m(GAP_S), m(3600)),
                    _rule("cap", "<", [_phase("cool", "auto", cap - m(fan_s), 26), _phase("fan", "3", m(fan_s))],
                          m(GAP_S), 0),
                ],
            }
        },
    }


def hard_cap(minute_s=MINUTE_S):
    return _min(2 * (GAP_S + 120), minute_s, 30)


def session_settings(minute_s=MINUTE_S):
    # ac_ip stays None: the address comes from DAIKIN_IP, never from a file. At 15 s: cap 48, hold 16 (a remote press
    # to our takeover write), release 5 (no write), finish fan 16.
    return dict(
        config.default_settings(),
        hard_cap_min=hard_cap(minute_s),
        manual_hold_min=_min(GAP_S, minute_s, 10),
        manual_release_off_min=_min(75, minute_s, 5),
        finish_fan_min=_min(GAP_S, minute_s),
        poll_s=60,
        dry_run=False,
    )


def ac_text(ac):
    if ac is None:
        return "unreachable"
    # "on cool 26.0, fan auto"; off keeps its mode, target and fan: "off (fan, fan 3)".
    s = ac.mode
    if ac.target is not None:
        s += " %d.%d" % (ac.target // 10, ac.target % 10)
    if ac.fan is not None:
        s += ", fan " + ac.fan
    return "on " + s if ac.power else "off (%s)" % s


def _reading_text(r):
    if r is None:
        return "none yet"
    if r.ac is None:
        return "unreachable (%s)" % r.error
    s = ac_text(r.ac)
    if r.temp is not None:
        s += "; room %d.%d C" % (r.temp // 10, r.temp % 10)
    if r.hum is not None:
        s += " %d %%" % r.hum
    return s


def _cycle_text(ctl):
    c = ctl.cycle
    s = ctl.state
    if c is not None:
        s += ", %s" % (c["rule"]["name"] or c["kind"])
        if c["rule"]["phases"] and ctl.state in controller.CYCLING:
            s += " phase %d/%d" % (c["phase"] + 1, len(c["rule"]["phases"]))
    return s


def ask(question):
    # The terminal prompt: y or yes approves; anything else, or no terminal, declines.
    try:
        answer = input(question)
    except EOFError:
        answer = ""
    return answer.strip().lower() in ("y", "yes")


def utc():
    y, mo, d, h, mi, s = time.gmtime()[:6]
    return "%04d-%02d-%02dT%02d:%02d:%02dZ" % (y, mo, d, h, mi, s)


class SessionLog:
    # session.log in the session dir: UTC time, then the message; printed as well.
    def __init__(self, root):
        self.path = root.rstrip("/") + "/session.log"

    def __call__(self, msg):
        line = "%s %s" % (utc(), msg)
        print(line)
        with open(self.path, "a") as f:
            f.write(line + "\n")


class Gate:
    # A DaikinClient behind the owner's approval and the fault toggle. ip and limits are the wrapped client's, which
    # App._set_client compares.
    def __init__(self, client, app):
        self.client = client
        self.app = app
        self.ip = client.ip
        self.limits = client.limits

    async def read(self):
        if self.app.fault:
            return Reading.unreachable("fault")
        return await self.client.read()

    async def write(self, state):
        app = self.app
        n = app.writes + 1
        want = ac_text(state)
        tick = app.last_reading
        question = "--- write %d (%s, up %d s%s)\n  read:  %s\n  write: %s\napprove? [y/N] " % (
            n, _cycle_text(app.controller), app.uptime_ms // 1000, self._gap_text(), _reading_text(tick), want)
        if not app.approve(question):
            return self._decided("declined", "write %d declined: %s" % (n, want), "declined")
        fresh = await self.read()
        if fresh.ac is None or tick is None or fresh.ac != tick.ac:
            msg = "write %d not sent, AC %s since the tick: %s" % (n, ac_text(fresh.ac), want)
            return self._decided("not sent", msg, "changed while awaiting approval")
        err = await self.client.write(state)
        app.last_sent_ms = app.board.ticks_ms()
        return self._decided("approved", "write %d approved: %s (%s)" % (n, want, err or "accepted"), err)

    def _gap_text(self):
        # ", last write 4:05 ago", flagged when under GAP_S: the owner's check on the session config.
        then = self.app.last_sent_ms
        if then is None:
            return ""
        s = ticks_since(self.app.board.ticks_ms(), then) // 1000
        return ", last write %d:%02d ago%s" % (s // 60, s % 60, " (under %d min!)" % (GAP_S // 60) if s < GAP_S else "")

    def _decided(self, kind, msg, err):
        self.app.decisions[kind] += 1
        self.app.session(msg)
        return err


class SupervisedApp(App):
    # App(shadow=False) with every client wrapped in a Gate, a new one after a settings save included.
    # approve(question) -> bool; session(msg) records a decision (SessionLog). App.log is the heap line's.
    def __init__(self, board, store, ac_ip, ac_port, approve, session):
        self.approve = approve
        self.session = session
        self.fault = False  # POST /run/fault
        self.decisions = {"approved": 0, "declined": 0, "not sent": 0}
        self.last_sent_ms = None  # board ticks at our last approved write
        super().__init__(board, store, ac_ip, ac_port)
        self.events.flash_kinds = FLASH_KINDS

    def _set_client(self, settings):
        old = self.client
        super()._set_client(settings)
        if self.client is not None and self.client is not old:
            self.client = Gate(self.client, self)


def add_routes(web, app, log, device=None):
    # The runner's own routes on the api.make() server, token as for the API. device: the FakeDaikin (--sim).
    def refused(req):
        return req.headers.get("X-Token") != TOKEN

    def body(req):
        # req.json wants Content-Type application/json; curl -d sends a form type, so parse the body itself.
        try:
            doc = json.loads(req.body.decode())
        except (AttributeError, UnicodeError, ValueError):
            doc = None
        return doc if isinstance(doc, dict) else None

    @web.post("/run/action")
    async def action(req):
        if refused(req):
            return {"error": "token missing or wrong"}, 401
        doc = body(req)
        if doc is None or not isinstance(doc.get("action"), str):
            return {"error": 'body must be {"action": ...}'}, 400
        if not app.action(doc):
            return {"error": "busy, try again"}, 503
        log("action %s" % doc)
        return app.status(), 202

    @web.post("/run/fault")
    async def fault(req):
        if refused(req):
            return {"error": "token missing or wrong"}, 401
        doc = body(req)
        mode = None if doc is None else doc.get("read")
        if mode not in ("unreachable", "ok"):
            return {"error": 'body must be {"read": "unreachable" | "ok"}'}, 400
        app.fault = mode == "unreachable"
        log("fault: reads %s" % mode)
        return {"fault": app.fault}

    @web.post("/run/sim")
    async def poke(req):
        if refused(req):
            return {"error": "token missing or wrong"}, 401
        if device is None:
            return {"error": "only with --sim"}, 409
        doc = body(req) or {}
        remote = doc.get("remote")
        minutes = doc.get("unreachable")
        try:
            if isinstance(remote, dict):
                target = remote.get("target_c")
                ac = ACState(remote["power"] is True, remote["mode"], None if target is None else tenths(target),
                             remote.get("fan"))
                device.remote(ac)
                log("sim: remote %s" % ac_text(ac))
            elif isinstance(minutes, (int, float)) and not isinstance(minutes, bool) and minutes > 0:
                device.unreachable(minutes)
                log("sim: AC silent for %g min" % minutes)
            else:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            return {"error": 'body must be {"remote": {power, mode, target_c, fan}} or {"unreachable": min}'}, 400
        return {"ac": ac_text(device.ac())}


class _Direct:
    # FakeDaikin without HTTP, for the exit check (--sim): the loop that served it is gone by then.
    def __init__(self, device, limits):
        self.device = device
        self.limits = limits

    async def read(self):
        doc = self.device.handle(codec.READ_REQUEST)
        return Reading.unreachable("no reply") if doc is None else codec.decode(doc, self.limits)

    async def write(self, state):
        doc = self.device.handle(codec.write_request(state))
        return "no reply" if doc is None else codec.write_result(doc)


async def leave(client, approve, log):
    # Ctrl-C: the AC as it is now; if it is on, an off write only on y. Leaving it on is the "Pico switched off
    # mid-cycle" case.
    r = await client.read()
    log("exit: AC %s" % _reading_text(r))
    if r.ac is None or not r.ac.power:
        return
    if not approve("switch the AC off? [y/N] "):
        log("exit: left on")
        return
    err = await client.write(ACState(False, r.ac.mode, r.ac.target, r.ac.fan))
    await asyncio.sleep_ms(SETTLE_MS)
    back = await client.read()
    log("exit: off write approved (%s), read back %s" % (err or "accepted", ac_text(back.ac)))


def _makedirs(path):
    so_far = ""
    for part in path.rstrip("/").split("/"):
        so_far += part + "/"
        if part in ("", "."):
            continue
        try:
            os.mkdir(so_far)
        except OSError:
            pass  # already there


def _seed(store, minute_s):
    # The session's config on first start; a restart keeps what is there (a PUT may have changed it).
    if store.load("profiles") is None:
        store.save("profiles", session_profiles(minute_s))
    if store.load("settings") is None:
        store.save("settings", session_settings(minute_s))


def _load_sim(store, device):
    doc = store.load("sim_ac")
    if isinstance(doc, dict) and doc.get("power") in ("00", "01") and isinstance(doc.get("e3001"), dict):
        device.power = doc["power"]
        device.e3001.update(doc["e3001"])


def _save_sim(store, device):
    store.save("sim_ac", {"power": device.power, "e3001": device.e3001})


async def _run(app, device, port, ac_port):
    if device is not None:
        await serve(device, port=ac_port)
    web = api.make(app, ROOT + "/web", token=TOKEN)
    add_routes(web, app, app.session, device)
    print("http://127.0.0.1:%d  token %r" % (port, TOKEN))
    await asyncio.gather(app.control(), tasks.housekeeping(app.board, app), web.start_server("127.0.0.1", port))


def main(argv):
    root = None
    minute_s = MINUTE_S
    sim = False
    port = PORT
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--minute-s" and i + 1 < len(argv):
            try:
                minute_s = float(argv[i + 1])
            except ValueError:
                minute_s = 0
            i += 1
        elif a == "--port" and i + 1 < len(argv):
            try:
                port = int(argv[i + 1])
            except ValueError:
                port = 0
            i += 1
        elif a == "--sim":
            sim = True
        elif root is None and not a.startswith("-"):
            root = a
        else:
            root = None
            break
        i += 1
    if root is None or not 5 <= minute_s <= 60 or not 1024 <= port <= 65535 - (AC_PORT - PORT):
        print("usage: micropython sim/supervised.py <dir> [--minute-s N (5-60)] [--sim] [--port N]")
        return 2
    ip = "127.0.0.1" if sim else os.getenv("DAIKIN_IP")
    if not ip:
        print("DAIKIN_IP is not set (the AC's IPv4 address); --sim runs against the simulator")
        return 2
    _makedirs(root)
    store = FileStore(root)
    _seed(store, minute_s)
    log = SessionLog(root)
    scale_minute(minute_s)
    device = None
    ac_port = 80
    if sim:
        device = FakeDaikin(time.ticks_ms)
        _load_sim(store, device)
        ac_port = AC_PORT + port - PORT
    app = SupervisedApp(HostBoard(), store, ip, ac_port, ask, log)
    log("start: boot %d, minute %g s, %s, files in %s" % (app.boot, minute_s, "simulator" if sim else "real AC", root))
    try:
        asyncio.run(_run(app, device, port, ac_port))
    except KeyboardInterrupt:
        print()
        # Tasks may be stuck mid-request (a held lock): a new loop and a new client, never the app's.
        asyncio.new_event_loop()
        limits = app.settings["sensor_limits"]
        client = _Direct(device, limits) if sim else DaikinClient(app.client.ip, ac_port, limits=limits)
        try:
            asyncio.run(leave(client, ask, log))
        except KeyboardInterrupt:
            print()
        if sim:
            _save_sim(store, device)
    log("end: boot %d, writes %s" % (app.boot, ", ".join("%s %d" % kv for kv in sorted(app.decisions.items()))))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
