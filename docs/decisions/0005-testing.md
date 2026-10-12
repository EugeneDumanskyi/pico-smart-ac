# 0005: Test strategy and tooling

- Status: accepted
- Date: 2026-10-08

## Context

The firmware runs only on MicroPython. Testing it on CPython as well would add debugger and coverage comfort,
but every module would have to work in both runtimes (shims for `time.ticks_*`, `micropython.const`, `asyncio`
differences), and a pass on CPython says nothing about the Pico. pytest does not run on MicroPython.
The real AC must be switched as little as possible, and every write to it needs the owner's approval.

## Decision

- **One test runtime: the MicroPython Unix port**, built from the pinned tag (v1.29.0, [firmware.md](../firmware.md)),
  on the Mac and in CI (cached). Product code and all its tests run only on MicroPython.
- **CPython runs tooling only**: `tools/` (status script, web build, deploy via `mpremote`) and ruff.
- Tests use micropython-lib `unittest` (vendored in `tests/lib/`, pinned). One runner, `tests/run.py`, runs
  `tests/unit`, `tests/golden` and `tests/integration`.
- **Golden traces**: JSON files with a config, a minute-by-minute list of readings and actions, and the expected
  commands and states. They are the behaviour contract; any change to them is reviewed like a spec change.
- **Simulator** (`sim/`, MicroPython): a fake `/dsiot/multireq` server (asyncio) with a simple room model (heat gain,
  cooling and drying rates, sensor rounding to whole degrees) and scripted "remote presses". It runs in the same
  process as the app and shares its fake clock, so time can be accelerated; the app still reaches it over HTTP.
- **Real AC** tests are manual, short, supervised, and logged in `docs/DAIKIN_API.md` or the milestone notes.
  The supervised runner (M4) is the app on the Unix port on the Mac.
- **Device** tests are `mpremote` scripts in `tests/device/` and a soak checklist (heap, reconnects, resets).
- CI: GitHub Actions on Linux: ruff, build the Unix port, run `tests/run.py`, build the web assets.

## Consequences

- No pytest, no coverage tool, no CPython debugger for product code; slightly more verbose tests. Accepted.
- No compatibility layer: code may use MicroPython features (`time.ticks_*`, `const`, `array`) directly.
- The Unix port is not the Pico: floats are double precision there and single precision on the rp2 port,
  and it has far more RAM. Temperatures are kept in whole tenths (integers) in the core, and RAM and
  hardware behaviour are covered only by device tests and the soak.
- Hardware-only code (`board/pico.py`, Wi-Fi supervisor details) is covered only by device tests and the soak.
- The Unix port's ticks period is 2^62 ms, the Pico's 2^30 (about 12.4 days), so a host run never wraps by itself.
  The month run (`sim/harness.py`) gives each simulated boot a ticks offset that starts it 0–3 days before the wrap.
  The other half-period hazard, a stamp older than about 6 days, can't happen on the host at all; the core prunes
  such stamps (rule selector, event ring), and only device tests and the soak cover it on the Pico.
