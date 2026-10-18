# SPDX-License-Identifier: GPL-3.0-only
# Value types and constants shared by the core (docs/architecture.md).
# Units: temperatures are integer tenths of a degree C (ADR 0005), humidity is integer percent,
# times are monotonic milliseconds from the injected clock (ADR 0004).
import time

# AC modes, spelled as in profiles.json and /api/state.
MODE_FAN = "fan"
MODE_HEAT = "heat"
MODE_COOL = "cool"
MODE_AUTO = "auto"
MODE_DRY = "dry"
MODE_UNKNOWN = "unknown"  # the AC reported a mode code we don't know
MODES = (MODE_FAN, MODE_HEAT, MODE_COOL, MODE_AUTO, MODE_DRY)

# Fan speeds.
FAN_AUTO = "auto"
FAN_QUIET = "quiet"
FANS = (FAN_AUTO, FAN_QUIET, "1", "2", "3", "4", "5")

# Sensor names used in rule conditions.
SENSOR_TEMP = "temp"
SENSOR_HUM = "hum"
SENSOR_OUTDOOR = "outdoor_temp"
SENSORS = (SENSOR_TEMP, SENSOR_HUM, SENSOR_OUTDOOR)
TENTHS_SENSORS = (SENSOR_TEMP, SENSOR_OUTDOOR)  # stored in tenths; hum is whole percent

# Controller states (idea.md, state machine).
IDLE = "IDLE"
RUNNING = "RUNNING"
STOPPING = "STOPPING"
COOLDOWN = "COOLDOWN"
MANUAL = "MANUAL"
TAKEOVER = "TAKEOVER"
PAUSED = "PAUSED"
OBSERVE = "OBSERVE"  # the app reads only and runs no controller (shell/app.py, observe mode)

# User actions (POST /api/action, /api/active).
ACT_PAUSE = "pause"
ACT_RESUME = "resume"
ACT_RUN = "run"
ACT_STOP = "stop"
ACT_STOP_NOW = "stop_now"
ACT_CLEAR_MANUAL = "clear_manual"
ACT_SET_PROFILE = "set_profile"

# Commands the controller returns, as (kind, payload) tuples.
CMD_WRITE = "write"  # payload: ACState to write
CMD_PERSIST = "persist"  # payload: state snapshot for state.json
CMD_EVENT = "event"  # payload: (event kind, message)

# Event kinds (the "kind" field of /api/events).
EV_BOOT = "boot"
EV_CYCLE_START = "cycle_start"
EV_PHASE = "phase"
EV_CYCLE_END = "cycle_end"
EV_HARD_CAP = "hard_cap"
EV_MANUAL = "manual"
EV_TAKEOVER = "takeover"
EV_PAUSE = "pause"
EV_RESUME = "resume"
EV_PROFILE = "profile"
EV_UNREACHABLE = "unreachable"
EV_WRITE_FAILED = "write_failed"
EV_WOULD = "would"  # dry_run: a write that was not sent
EV_CONFIG_SAVED = "config_saved"
EV_IGNORED = "ignored"  # an action that doesn't apply in the current state
EV_LOW_MEMORY = "low_memory"  # free heap or the largest block fell below its threshold (shell/tasks.py)
EV_HEAP = "heap"  # hourly free heap and largest block, to events.log only (shell/tasks.py)
EV_BLOCKED = "blocked"  # shadow mode: a write reached the shell and was not sent (shell/app.py)


def tenths(celsius):
    # A config value in degrees C (int or float, e.g. 26 or 26.5) -> integer tenths.
    if isinstance(celsius, int):
        return celsius * 10
    return int(round(celsius * 10))


def ticks_since(now, then):
    # Milliseconds from `then` to `now`; the one place monotonic ticks are compared (ADR 0004).
    return time.ticks_diff(now, then)


class ACState:
    # What the AC is set to. target is tenths of C, None in modes without a setpoint (fan, dry);
    # fan is one of FANS, None if unknown.
    def __init__(self, power, mode, target=None, fan=None):
        self.power = power
        self.mode = mode
        self.target = target
        self.fan = fan

    def __eq__(self, other):
        return (
            isinstance(other, ACState)
            and self.power == other.power
            and self.mode == other.mode
            and self.target == other.target
            and self.fan == other.fan
        )

    def __ne__(self, other):
        return not self.__eq__(other)

    def __repr__(self):
        return "ACState(%r, %r, %r, %r)" % (self.power, self.mode, self.target, self.fan)


class Reading:
    # One poll of the AC. ac is None when the AC could not be read (then error says why).
    # A sensor is None when it was missing or outside its sanity range.
    def __init__(self, ac=None, temp=None, hum=None, outdoor_temp=None, error=None):
        self.ac = ac
        self.temp = temp
        self.hum = hum
        self.outdoor_temp = outdoor_temp
        self.error = error

    @staticmethod
    def unreachable(error):
        return Reading(error=error)

    @property
    def reachable(self):
        return self.ac is not None

    def sensor(self, name):
        if name == SENSOR_TEMP:
            return self.temp
        if name == SENSOR_HUM:
            return self.hum
        if name == SENSOR_OUTDOOR:
            return self.outdoor_temp
        return None

    def __repr__(self):
        return "Reading(%r, temp=%r, hum=%r, outdoor_temp=%r, error=%r)" % (
            self.ac,
            self.temp,
            self.hum,
            self.outdoor_temp,
            self.error,
        )
