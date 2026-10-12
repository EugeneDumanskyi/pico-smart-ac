# SPDX-License-Identifier: GPL-3.0-only
# Built-in defaults for profiles.json and settings.json (docs/idea.md, "Profiles (defaults)" and
# "Config files"). Used when a file is missing or broken, and by "Restore defaults".
# Each call returns a fresh document, so callers may change it freely.

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
                            _phase("cool", "auto", 30, _cond("temp", "<=", 26), target_c=26),
                            _phase("fan", "3", 20),
                        ],
                        cooldown_min=30,
                    ),
                    _rule(
                        "humid",
                        _when(_cond("hum", ">", 75)),
                        [
                            _phase("dry", "auto", 60, _cond("hum", "<=", 65)),
                            _phase("fan", "3", 20),
                        ],
                        cooldown_min=45,
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
                            _phase("cool", "quiet", 45, _cond("temp", "<=", 26), target_c=26),
                            _phase("fan", "quiet", 15),
                        ],
                        cooldown_min=30,
                    ),
                    _rule(
                        "humid",
                        _when(_cond("hum", ">", 75)),
                        [
                            _phase("dry", "quiet", 45, _cond("hum", "<=", 65)),
                            _phase("fan", "quiet", 15),
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
                            _phase("dry", "auto", 30),
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
