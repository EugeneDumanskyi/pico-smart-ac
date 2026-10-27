# SPDX-License-Identifier: GPL-3.0-only
# profiles.json and settings.json (docs/idea.md, "Config files"): built-in defaults, used when a file is
# missing or broken and by "Restore defaults", and validation of a saved document.
# Each default_*() call returns a fresh document, so callers may change it freely.
import json

from smartac.core.types import FANS

VERSION = 1
OFF = "off"  # the kill switch: a value of "active", not an entry in "profiles"


def _cond(sensor, op, value):
    return {"sensor": sensor, "op": op, "value": value}


def _when(*conditions):
    return {"match": "all", "conditions": list(conditions)}


def _phase(mode, fan, max_min, until=None, target_c=None):
    p = {"mode": mode, "fan": fan, "max_min": max_min, "until": until}
    if target_c is not None:
        p["target_c"] = target_c
    return p


def _rule(name, when, phases, cooldown_min, min_interval_min=0, confirm_reads=2):
    return {
        "name": name,
        "when": when,
        "confirm_reads": confirm_reads,
        "phases": phases,
        "cooldown_min": cooldown_min,
        "min_interval_min": min_interval_min,
    }


def default_profiles():
    return {
        "version": VERSION,
        "active": OFF,
        "profiles": {
            "day": {
                "label": "Day",
                "rules": [
                    _rule(
                        "too_hot",
                        _when(_cond("temp", ">", 29)),
                        [
                            _phase("cool", "3", 30, _cond("temp", "<=", 26), target_c=26),
                            _phase("fan", "3", 20),
                        ],
                        cooldown_min=20,
                    ),
                    _rule(
                        "humid",
                        _when(_cond("hum", ">", 75)),
                        [
                            _phase("cool", "3", 60, _cond("hum", "<=", 65), target_c=26),
                            _phase("fan", "3", 20),
                        ],
                        cooldown_min=30,
                    ),
                ],
            },
            "night": {
                "label": "Night",
                "rules": [
                    _rule(
                        "warm",
                        _when(_cond("temp", ">", 27)),
                        [
                            _phase("cool", "3", 20, _cond("temp", "<=", 25), target_c=25),
                            _phase("fan", "3", 15),
                        ],
                        cooldown_min=30,
                    ),
                    _rule(
                        "humid",
                        _when(_cond("hum", ">", 75)),
                        [
                            _phase("dry", "3", 45, _cond("hum", "<=", 65)),
                            _phase("fan", "3", 15),
                        ],
                        cooldown_min=45,
                    ),
                ],
            },
            "away": {
                "label": "Away",
                "rules": [
                    _rule(
                        "damp",
                        _when(_cond("hum", ">", 80)),
                        [
                            _phase("dry", "3", 30),
                            _phase("fan", "3", 20),
                        ],
                        cooldown_min=0,
                        min_interval_min=240,
                    ),
                ],
            },
        },
    }


def default_settings(ac_ip=None):
    # ac_ip comes from DAIKIN_IP in secrets.py; the core never reads it itself.
    return {
        "version": VERSION,
        "ac_ip": ac_ip,
        "poll_s": 60,
        "hard_cap_min": 120,
        "manual_hold_min": 120,
        "manual_release_off_min": 30,
        "finish_fan_min": 20,
        "sensor_limits": {"temp": [10, 45], "hum": [5, 100]},
        "dry_run": False,
    }


# --- Validation (docs/idea.md, "Validation") ---
# validate_*() return (errors, warnings), each a list of {"path": "profiles.day.rules[0].name", "msg": "..."}.
# Any error rejects the whole save; warnings are shown but don't block it.

MAX_DOC_BYTES = 16 * 1024
NAME_CHARS = "abcdefghijklmnopqrstuvwxyz0123456789_"
MAX_PROFILES = 10
MAX_LABEL = 32
OPS = (">", ">=", "<", "<=")
FALLING_OPS = ("<", "<=")
PHASE_MODES = ("cool", "dry", "fan")  # heat and auto are not offered in v1
CONDITION_LIMITS = {"temp": (10, 45), "outdoor_temp": (10, 45), "hum": (20, 95)}
# Sensors an `until` goal can sensibly wait for, per mode (both fall while the mode runs).
GOAL_SENSORS = {"cool": ("temp", "hum"), "dry": ("hum", "temp")}

PROFILES_KEYS = ("version", "active", "profiles")
PROFILE_KEYS = ("label", "rules")
RULE_KEYS = ("name", "when", "confirm_reads", "phases", "cooldown_min", "min_interval_min")
WHEN_KEYS = ("match", "conditions")
COND_KEYS = ("sensor", "op", "value")
PHASE_KEYS = ("mode", "fan", "max_min", "until", "target_c")

# Settings ranges: the ones in idea.md's table, plus our picks for fields the table doesn't cover.
SETTINGS_INTS = {
    "poll_s": (30, 300),
    "hard_cap_min": (30, 480),
    "manual_hold_min": (10, 480),
    "manual_release_off_min": (5, 240),
    "finish_fan_min": (0, 60),
}
SETTINGS_KEYS = ("version", "ac_ip", "sensor_limits", "dry_run") + tuple(SETTINGS_INTS)
SENSOR_LIMIT_BOUNDS = {"temp": (0, 60), "hum": (0, 100)}


class _Report:
    def __init__(self):
        self.errors = []
        self.warnings = []

    def error(self, path, msg):
        self.errors.append({"path": path, "msg": msg})

    def warn(self, path, msg):
        self.warnings.append({"path": path, "msg": msg})


def _join(path, key):
    if isinstance(key, int):
        return "%s[%d]" % (path, key)
    return "%s.%s" % (path, key) if path else key


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _is_num(v):
    return (isinstance(v, int) or isinstance(v, float)) and not isinstance(v, bool)


def _obj(rep, path, v, keys, required=None):
    # A dict with only known keys and all required ones (default: all keys). False if not a dict.
    if not isinstance(v, dict):
        rep.error(path or "$", "must be an object")
        return False
    for k in v:
        if k not in keys:
            rep.error(_join(path, k), "unknown field")
    for k in keys if required is None else required:
        if k not in v:
            rep.error(_join(path, k), "required")
    return True


def _int_in(rep, path, v, lo, hi):
    if not _is_int(v) or v < lo or v > hi:
        rep.error(path, "must be a whole number %d–%d" % (lo, hi))
        return False
    return True


def _list_len(rep, path, v, lo, hi, what):
    if not isinstance(v, list):
        rep.error(path, "must be a list")
        return False
    if len(v) < lo or len(v) > hi:
        rep.error(path, "%d–%d %s" % (lo, hi, what))
        return False
    return True


def _check_name(rep, path, v):
    if not isinstance(v, str) or not 1 <= len(v) <= 24 or any(ch not in NAME_CHARS for ch in v):
        rep.error(path, "1–24 characters: a–z, 0–9, _")
        return False
    return True


def _check_condition(rep, path, c):
    if not _obj(rep, path, c, COND_KEYS):
        return
    sensor = c.get("sensor")
    if sensor not in CONDITION_LIMITS:
        rep.error(_join(path, "sensor"), "must be one of " + ", ".join(CONDITION_LIMITS))
    if c.get("op") not in OPS:
        rep.error(_join(path, "op"), "must be one of " + " ".join(OPS))
    if "value" in c and sensor in CONDITION_LIMITS:
        lo, hi = CONDITION_LIMITS[sensor]
        v = c["value"]
        if not _is_num(v) or v < lo or v > hi:
            rep.error(_join(path, "value"), "must be %d–%d" % (lo, hi))


def _half_step(v):
    return _is_num(v) and v * 2 == int(v * 2)


def _check_phase(rep, path, p):
    if not _obj(rep, path, p, PHASE_KEYS, ("mode", "fan", "max_min")):
        return
    mode = p.get("mode")
    if mode not in PHASE_MODES:
        rep.error(_join(path, "mode"), "must be one of " + ", ".join(PHASE_MODES))
    if p.get("fan") not in FANS:
        rep.error(_join(path, "fan"), "must be one of " + ", ".join(FANS))
    if "max_min" in p:
        _int_in(rep, _join(path, "max_min"), p["max_min"], 1, 120)
    target = p.get("target_c")
    if mode == "cool":
        if not _half_step(target) or target < 18 or target > 30:
            rep.error(_join(path, "target_c"), "must be 18–30, step 0.5")
    elif target is not None:
        rep.error(_join(path, "target_c"), "only for cool")
    until = p.get("until")
    if until is not None:
        upath = _join(path, "until")
        n = len(rep.errors)
        _check_condition(rep, upath, until)
        if len(rep.errors) == n and mode in PHASE_MODES:
            if until["sensor"] not in GOAL_SENSORS.get(mode, ()) or until["op"] not in FALLING_OPS:
                rep.warn(upath, "%s may never reach this goal; the phase will run its full time" % mode)


def _check_rule(rep, path, r, hard_cap_min):
    if not _obj(rep, path, r, RULE_KEYS):
        return None
    _check_name(rep, _join(path, "name"), r.get("name"))
    when = r.get("when")
    wpath = _join(path, "when")
    if _obj(rep, wpath, when, WHEN_KEYS):
        if when.get("match") not in ("all", "any"):
            rep.error(_join(wpath, "match"), "must be all or any")
        cpath = _join(wpath, "conditions")
        if "conditions" in when and _list_len(rep, cpath, when["conditions"], 1, 4, "conditions"):
            for i, c in enumerate(when["conditions"]):
                _check_condition(rep, _join(cpath, i), c)
    if "confirm_reads" in r:
        _int_in(rep, _join(path, "confirm_reads"), r["confirm_reads"], 1, 10)
    if "cooldown_min" in r:
        _int_in(rep, _join(path, "cooldown_min"), r["cooldown_min"], 0, 480)
    if "min_interval_min" in r:
        _int_in(rep, _join(path, "min_interval_min"), r["min_interval_min"], 0, 1440)
    phases = r.get("phases")
    ppath = _join(path, "phases")
    if "phases" in r and _list_len(rep, ppath, phases, 1, 5, "phases"):
        for i, p in enumerate(phases):
            _check_phase(rep, _join(ppath, i), p)
        _phase_totals(rep, ppath, phases, hard_cap_min)
    return r.get("name")


def _rule_minutes(phases):
    # Sum of max_min over the phases, or None if any is not a whole number.
    if not isinstance(phases, list):
        return None
    mins = [p.get("max_min") if isinstance(p, dict) else None for p in phases]
    if not mins or not all(_is_int(m) for m in mins):
        return None
    return sum(mins)


def _phase_totals(rep, path, phases, hard_cap_min):
    total = _rule_minutes(phases)
    if total is not None and total > hard_cap_min:
        rep.error(path, "phases add up to %d min, more than hard_cap_min (%d)" % (total, hard_cap_min))
    modes = [p.get("mode") for p in phases if isinstance(p, dict)]
    if modes and ("cool" in modes or "dry" in modes) and modes[-1] != "fan":
        rep.warn(path, "ends without a fan phase; the coil stays wet")


def validate_profiles(doc, hard_cap_min=120):
    # profiles.json as parsed from a save. hard_cap_min comes from the current settings.
    rep = _Report()
    if not _obj(rep, "", doc, PROFILES_KEYS):
        return rep.errors, rep.warnings
    try:
        if len(json.dumps(doc)) > MAX_DOC_BYTES:
            rep.error("$", "document larger than 16 KB")
    except (TypeError, ValueError):
        rep.error("$", "not JSON")
    if "version" in doc and doc["version"] != VERSION:
        rep.error("version", "must be %d" % VERSION)
    profiles = doc.get("profiles")
    names = []
    if isinstance(profiles, dict):
        if len(profiles) > MAX_PROFILES:
            rep.error("profiles", "at most %d profiles" % MAX_PROFILES)
        for pname, prof in profiles.items():
            ppath = _join("profiles", pname)
            if _check_name(rep, ppath, pname):
                if pname == OFF:
                    rep.error(ppath, '"off" is reserved')
                names.append(pname)
            if not _obj(rep, ppath, prof, PROFILE_KEYS, ("rules",)):
                continue
            label = prof.get("label")
            if label is not None and (not isinstance(label, str) or len(label) > MAX_LABEL):
                rep.error(_join(ppath, "label"), "text, at most %d characters" % MAX_LABEL)
            rpath = _join(ppath, "rules")
            if "rules" in prof and _list_len(rep, rpath, prof["rules"], 1, 8, "rules"):
                seen = []
                for i, r in enumerate(prof["rules"]):
                    rname = _check_rule(rep, _join(rpath, i), r, hard_cap_min)
                    if isinstance(rname, str) and rname in seen:
                        rep.error(_join(_join(rpath, i), "name"), "duplicate rule name")
                    seen.append(rname)
    elif "profiles" in doc:
        rep.error("profiles", "must be an object")
    if "active" in doc and doc["active"] != OFF and doc["active"] not in names:
        rep.error("active", 'must be "off" or a profile name')
    return rep.errors, rep.warnings


def validate_settings(settings, profiles=None):
    # settings.json as parsed from a save. With the current profiles document, a lower hard_cap_min
    # that a rule no longer fits is an error too.
    rep = _Report()
    if not _obj(rep, "", settings, SETTINGS_KEYS):
        return rep.errors, rep.warnings
    if "version" in settings and settings["version"] != VERSION:
        rep.error("version", "must be %d" % VERSION)
    ip = settings.get("ac_ip")
    if ip is not None and not is_ipv4(ip):
        rep.error("ac_ip", "must be an IPv4 address, e.g. 192.0.2.10")
    for key, (lo, hi) in SETTINGS_INTS.items():
        if key in settings:
            _int_in(rep, key, settings[key], lo, hi)
    if "dry_run" in settings and not isinstance(settings["dry_run"], bool):
        rep.error("dry_run", "must be true or false")
    limits = settings.get("sensor_limits")
    if "sensor_limits" in settings and _obj(rep, "sensor_limits", limits, tuple(SENSOR_LIMIT_BOUNDS)):
        for key, (lo, hi) in SENSOR_LIMIT_BOUNDS.items():
            if key not in limits:
                continue  # reported as required
            pair = limits[key]
            path = _join("sensor_limits", key)
            ok = isinstance(pair, list) and len(pair) == 2 and all(_is_num(v) for v in pair)
            if not ok or not lo <= pair[0] < pair[1] <= hi:
                rep.error(path, "must be [low, high] within %d–%d" % (lo, hi))
    cap = settings.get("hard_cap_min")
    if _is_int(cap) and isinstance(profiles, dict) and isinstance(profiles.get("profiles"), dict):
        for pname, prof in profiles["profiles"].items():
            rules = prof.get("rules") if isinstance(prof, dict) else None
            for i, r in enumerate(rules if isinstance(rules, list) else ()):
                total = _rule_minutes(r.get("phases")) if isinstance(r, dict) else None
                if total is not None and total > cap:
                    rep.error("hard_cap_min", "profiles.%s.rules[%d] needs %d min" % (pname, i, total))
    return rep.errors, rep.warnings


def is_ipv4(s):
    # A dotted IPv4 literal. The AC is reached by address only: a name would need DNS, which blocks the loop.
    if not isinstance(s, str):
        return False
    parts = s.split(".")
    if len(parts) != 4:
        return False
    for part in parts:
        if not part or len(part) > 3 or any(ch not in "0123456789" for ch in part) or int(part) > 255:
            return False
    return True
