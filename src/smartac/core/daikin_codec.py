# SPDX-License-Identifier: GPL-3.0-only
# Daikin dsiot encoding (docs/DAIKIN_API.md). Read side: parsed multireq JSON -> Reading.
# Pure: the shell does the HTTP POST and json.loads, this module only maps values.
from smartac.core.types import (
    FAN_AUTO,
    FAN_QUIET,
    MODE_AUTO,
    MODE_COOL,
    MODE_DRY,
    MODE_FAN,
    MODE_HEAT,
    MODE_UNKNOWN,
    SENSOR_HUM,
    SENSOR_TEMP,
    ACState,
    Reading,
    tenths,
)

INDOOR = "/dsiot/edge/adr_0100.dgc_status"
OUTDOOR = "/dsiot/edge/adr_0200.dgc_status"
READ_REQUEST = {
    "requests": [
        {"op": 2, "to": INDOOR + "?filter=pv,pt,md"},
        {"op": 2, "to": OUTDOOR + "?filter=pv,pt,md"},
    ]
}

RSC_READ_OK = 2000

MODE_CODES = {"0000": MODE_FAN, "0100": MODE_HEAT, "0200": MODE_COOL, "0300": MODE_AUTO, "0500": MODE_DRY}
FAN_CODES = {"0A00": FAN_AUTO, "0B00": FAN_QUIET, "0300": "1", "0400": "2", "0500": "3", "0600": "4", "0700": "5"}
# Per-mode parameters inside e_1002/e_3001.
SETPOINT_PARAM = {MODE_COOL: "p_02", MODE_HEAT: "p_03", MODE_AUTO: "p_1D"}
FAN_PARAM = {MODE_COOL: "p_09", MODE_HEAT: "p_0A", MODE_AUTO: "p_26", MODE_DRY: "p_27", MODE_FAN: "p_28"}

# Sanity ranges in degrees C / percent, inclusive. temp and hum can be overridden from
# settings.json sensor_limits; outdoor and target are fixed.
DEFAULT_LIMITS = {SENSOR_TEMP: (10, 45), SENSOR_HUM: (5, 100)}
OUTDOOR_LIMITS = (-30, 60)
TARGET_LIMITS = (10, 32)

_HEX = "0123456789abcdefABCDEF"


def le_int(hexstr, signed=False):
    # Little-endian hex string ("2E", "F6FF") -> int. Raises ValueError on anything else.
    if not isinstance(hexstr, str) or not hexstr or len(hexstr) % 2:
        raise ValueError("bad hex")
    for ch in hexstr:
        if ch not in _HEX:
            raise ValueError("bad hex")
    value = 0
    for i in range(len(hexstr) - 2, -1, -2):
        value = (value << 8) | int(hexstr[i : i + 2], 16)
    bits = len(hexstr) * 4
    if signed and value >= 1 << (bits - 1):
        value -= 1 << bits
    return value


def _child(node, name):
    if not isinstance(node, dict):
        return None
    children = node.get("pch")
    if not isinstance(children, list):
        return None
    for c in children:
        if isinstance(c, dict) and c.get("pn") == name:
            return c
    return None


def _pv(node, *names):
    # Value at a path of child names, or None if any step is missing.
    for name in names:
        node = _child(node, name)
        if node is None:
            return None
    return node.get("pv")


def _int(node, path, signed=False):
    pv = _pv(node, *path)
    if pv is None:
        return None
    try:
        return le_int(pv, signed)
    except ValueError:
        return None


def _in_range(value, limits):
    if value is None or value < limits[0] or value > limits[1]:
        return None
    return value


def _find_roots(response):
    # -> (indoor e_1002 node, outdoor e_1003 node, error). Roots are found by name in any
    # response that read OK, so the order of responses doesn't matter.
    if not isinstance(response, dict) or not isinstance(response.get("responses"), list):
        return None, None, "malformed response"
    indoor = outdoor = None
    error = None
    for r in response["responses"]:
        if not isinstance(r, dict):
            continue
        rsc = r.get("rsc")
        if rsc is not None and rsc != RSC_READ_OK:
            error = "rsc %s" % rsc
            continue
        pc = r.get("pc")
        indoor = indoor or _child(pc, "e_1002")
        outdoor = outdoor or _child(pc, "e_1003")
    return indoor, outdoor, error


def decode(response, limits=None):
    # Parsed /dsiot/multireq response for READ_REQUEST -> Reading. Never raises: a response
    # without usable indoor power/mode is unreachable; a missing or out-of-range sensor is None.
    # limits: settings sensor_limits ({"temp": [lo, hi], "hum": [lo, hi]}, degrees C / percent).
    indoor, outdoor, error = _find_roots(response)
    if indoor is None:
        return Reading.unreachable(error or "no indoor data")

    power = _pv(indoor, "e_A002", "p_01")
    if power not in ("00", "01"):
        return Reading.unreachable("bad power %r" % (power,))
    mode_code = _pv(indoor, "e_3001", "p_01")
    if mode_code is None:
        return Reading.unreachable("no mode")
    mode = MODE_CODES.get(mode_code, MODE_UNKNOWN)

    target = None
    if mode in SETPOINT_PARAM:
        raw = _int(indoor, ("e_3001", SETPOINT_PARAM[mode]))
        if raw is not None:
            target = _in_range(raw * 5, (tenths(TARGET_LIMITS[0]), tenths(TARGET_LIMITS[1])))
    fan = None
    if mode in FAN_PARAM:
        fan = FAN_CODES.get(_pv(indoor, "e_3001", FAN_PARAM[mode]))
    ac = ACState(power == "01", mode, target, fan)

    lim = limits or DEFAULT_LIMITS
    t_lim = lim.get(SENSOR_TEMP, DEFAULT_LIMITS[SENSOR_TEMP])
    h_lim = lim.get(SENSOR_HUM, DEFAULT_LIMITS[SENSOR_HUM])

    temp = _int(indoor, ("e_A00B", "p_01"), signed=True)
    temp = _in_range(None if temp is None else temp * 10, (tenths(t_lim[0]), tenths(t_lim[1])))
    hum = _in_range(_int(indoor, ("e_A00B", "p_02")), h_lim)
    out = None
    if outdoor is not None:
        out = _int(outdoor, ("e_A00D", "p_01"), signed=True)
        out = _in_range(None if out is None else out * 5, (tenths(OUTDOOR_LIMITS[0]), tenths(OUTDOOR_LIMITS[1])))
    return Reading(ac, temp=temp, hum=hum, outdoor_temp=out)
