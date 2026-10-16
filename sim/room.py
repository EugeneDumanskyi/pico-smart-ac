# SPDX-License-Identifier: GPL-3.0-only
# Room model: temperature, humidity and a wet coil, driven by the AC state. Floats are fine here (Unix port only).
# Calibrated to one real run (docs/DAIKIN_API.md, 2026-10-08): cool 10 min took 29 -> 27 C and 80 -> 70 %, then
# humidity came back to 75-80 % in fan mode and after off. It tests the controller's logic, not comfort.
import math

from smartac.core.types import MODE_COOL, MODE_DRY

# Rates are per minute.
ROOM_DEFAULTS = {
    "temp": 29.0,  # start, C
    "hum": 80.0,  # start, %
    "outdoor": 32.0,  # C, constant unless set
    "leak": 0.01,  # temp -> outdoor: dT += leak * (outdoor - T), time constant ~100 min
    "hum_ambient": 80.0,  # % that humidity drifts back to
    "hum_leak": 0.02,  # dH += hum_leak * (hum_ambient - H)
    "cool_rate": 0.25,  # C/min in cool, x fan factor x clamp(T - target, 0, 1)
    "cool_dry": 1.2,  # %/min removed in cool, same factors
    "dry_rate": 0.08,  # C/min in dry, x fan factor
    "dry_dry": 0.8,  # %/min removed in dry, x fan factor
    "fan_factor": {"quiet": 0.5, "1": 0.6, "2": 0.7, "3": 0.8, "4": 0.9, "5": 1.0, "auto": 1.0},
    "coil_keep": 0.4,  # part of the removed moisture that stays on the coil
    "coil_max": 8.0,  # % the coil can hold
    "coil_release_fan": 1.0,  # %/min the wet coil gives back with the AC on (fan, heat, auto)
    "coil_release_off": 0.3,  # %/min with the AC off
}


def _round(x):
    return int(math.floor(x + 0.5))


class Room:
    def __init__(self, params=None):
        p = dict(ROOM_DEFAULTS)
        p.update(params or {})
        self.p = p
        self.temp = float(p["temp"])
        self.hum = float(p["hum"])
        self.outdoor = float(p["outdoor"])
        self.coil = 0.0  # % of humidity held on the coil

    def set(self, temp=None, hum=None, outdoor=None):
        if temp is not None:
            self.temp = float(temp)
        if hum is not None:
            self.hum = float(hum)
        if outdoor is not None:
            self.outdoor = float(outdoor)

    def step(self, minutes, ac):
        # Advance by `minutes` (float ok) with the AC in state `ac` (ACState), Euler steps of at most 1 min.
        n = int(math.ceil(minutes))
        for _ in range(n):
            self._step(minutes / n, ac)

    def _step(self, dt, ac):
        p = self.p
        d_temp = p["leak"] * (self.outdoor - self.temp)
        d_hum = p["hum_leak"] * (p["hum_ambient"] - self.hum)
        removed = 0.0
        on = ac is not None and ac.power
        fan = p["fan_factor"].get(ac.fan, 1.0) if on else 0.0
        if on and ac.mode == MODE_COOL:
            gap = 1.0 if ac.target is None else min(max(self.temp - ac.target / 10, 0.0), 1.0)
            d_temp -= p["cool_rate"] * fan * gap
            removed = p["cool_dry"] * fan * gap
        elif on and ac.mode == MODE_DRY:
            d_temp -= p["dry_rate"] * fan
            removed = p["dry_dry"] * fan
        if removed:
            d_hum -= removed
            self.coil = min(self.coil + p["coil_keep"] * removed * dt, p["coil_max"])
        elif self.coil > 0:
            rate = p["coil_release_fan"] if on else p["coil_release_off"]
            back = min(self.coil, rate * dt)
            self.coil -= back
            d_hum += back / dt
        self.temp += d_temp * dt
        self.hum = min(max(self.hum + d_hum * dt, 0.0), 100.0)

    def sensors(self):
        # What the AC reports: room and outdoor temp in half degrees, whole % (0-100).
        return {
            "temp_half": _round(self.temp * 2),
            "hum": min(max(_round(self.hum), 0), 100),
            "outdoor_half": _round(self.outdoor * 2),
        }
