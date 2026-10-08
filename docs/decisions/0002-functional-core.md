# 0002: Functional core, async shell, hardware behind interfaces

- Status: accepted
- Date: 2026-10-08

## Context

The logic that decides when the AC runs is the part that can do harm, and it must be tested far more often
than the AC can be switched. It has to run on the Mac and in CI (MicroPython Unix port) and on the Pico.
[ADR 0001](0001-language.md) already asks for a logic core with the clock passed in.

## Decision

- `smartac/core/` is pure: no sockets, files, `machine`, `network` or `time` calls. Inputs are a monotonic time,
  the latest `Reading` and queued user actions. Output is a list of commands
  (write AC state, persist snapshot, emit event). The Daikin JSON codec is in the core too.
- `smartac/shell/` holds async adapters (Daikin client, store, Wi-Fi, HTTP API, task wiring).
- Hardware is reached only through a `Board` object (`board/base.py`): Wi-Fi, watchdog, LED, reset,
  reset cause. `board/pico.py` is real; `board/host.py` is a no-op/fake for the Unix port.
- The AC is reached only through the Daikin client's URL, so the simulator (`sim/`) replaces it without code changes.
- Product code targets MicroPython only; its tests run on the Unix port ([ADR 0005](0005-testing.md)).

## Consequences

- The whole app (HTTP API and UI included) runs on the Mac against the simulator, so most work needs neither the
  Pico nor the real AC.
- Golden traces (config + readings → commands) pin behaviour and survive refactors.
- A little extra indirection (the `Board` object, passing time in). Accepted.
