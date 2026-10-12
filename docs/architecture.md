# Architecture

How the firmware is built. Behaviour (profiles, state machine, UI, API) is specified in [idea.md](idea.md);
the plan is in [roadmap.md](roadmap.md). Decisions: [decisions/](decisions/).

## Principles

- **Functional core, async shell** ([ADR 0002](decisions/0002-functional-core.md)). All decisions live in pure code
  that takes readings and a clock and returns commands. I/O lives in thin async adapters.
- **One owner per resource.** Only the control task talks to the AC. Only the store writes flash.
  HTTP handlers read snapshots and queue actions; they never call the AC directly.
- **Remote wins** ([ADR 0003](decisions/0003-ac-ownership.md)). Automation only changes or switches off an AC state it
  set itself and still sees unchanged.
- **Fail towards "off and hands off"**: on doubt, finish our own cycle and stop; never start one.
- **Relative time only** ([ADR 0004](decisions/0004-time.md)): monotonic ticks for every duration; no NTP, no wall clock.

## Modules

| Module | Layer | Responsibility |
|---|---|---|
| `core/types.py` | core | `Reading` (AC state + sensors, or unreachable), `ACState`, command and event constants |
| `core/daikin_codec.py` | core | dsiot JSON ⇄ `ACState`/`Reading`: decode responses, build write requests, range checks |
| `core/config.py` | core | built-in defaults, validation with JSON-path errors and warnings, version migration |
| `core/rules.py` | core | condition evaluation, first-match rule selection, `confirm_reads` counting, `min_interval` |
| `core/controller.py` | core | state machine: `step(now, reading, actions) → commands`; owns cycle, stopping, cooldown, manual hold, takeover, pause |
| `core/history.py` | core | 1440 × 1-min ring buffer in preallocated `array`s; streamed JSON export |
| `core/events.py` | core | event ring (200), marks which events go to flash and owns the `events.log` line format |
| `shell/daikin.py` | shell | async HTTP POST to `/dsiot/multireq`, hard timeout, one request at a time |
| `shell/store.py` | shell | atomic JSON files (`.tmp` → rename, `.bak`), state snapshot, event log, crash log |
| `shell/net.py` | shell | Wi-Fi supervisor (power-save off, reconnect, counters) |
| `shell/api.py` | shell | Microdot routes from idea.md, token check, body size limit, streamed responses |
| `shell/app.py` | shell | wiring and the asyncio tasks below |
| `board/base.py` | shell | `Board` interface: Wi-Fi, watchdog, LED, reset, reset cause |
| `board/pico.py`, `board/host.py` | shell | real hardware / no-op + fake time for the Unix port |
| `sim/clock.py` | sim | `FakeClock` (moves only when told, can start near the ticks wrap), `ScaledClock` (real time × speed); any callable returning ticks works as the clock |
| `sim/room.py` | sim | room model: temperature, humidity and a wet coil per minute, sensors rounded like the AC's |
| `sim/device.py` | sim | `FakeDaikin`: raw indoor/outdoor params, reads, validated writes, scripted remote presses, outages, rejected or ignored writes; no I/O |
| `sim/server.py`, `sim/run.py` | sim | minimal HTTP/1.1 on 127.0.0.1 (one request per connection); standalone runner for manual pokes |
| `www/` | assets | single-page UI (vanilla JS, no framework, no CDN), gzipped at build time |

Logging: user-facing **events** (ring in RAM, cycle summaries and config saves appended to flash),
**debug log** to USB serial with a level setting, **crash log** (last exception + reset cause, one small file).

## Tasks (single asyncio loop)

| Task | Period | Does | Must not |
|---|---|---|---|
| `control` | `poll_s` (60 s), or at once when an action is queued | read AC → `controller.step` → write if commanded → read back after a settle delay → update snapshot, history, events, persist on transitions | run longer than ~5 s per tick (all calls have timeouts) |
| `net` | 5 s | check link, reconnect with backoff, reset if offline too long | call anything that resolves names (DNS blocks the loop) |
| `http` | on request | Microdot; read snapshot, queue actions, save config through `store` | call the AC, hold big strings (stream large responses) |
| `watchdog` | 1 s | feed the hardware WDT **only if** `control` beat within `2 × poll_s` and `net` beat within 30 s | feed unconditionally |
| `housekeeping` | 60 s | `gc.collect()`, heap stats (free, largest block), flush flash events | allocate |

Rules for code in this loop: no blocking call (no `requests`, no `time.sleep`), no `await` inside a
read-modify-write of shared state, no `_thread` / second core.

## Data flow

```
          ┌───────────── actions (queue + wake event) ◀──────────── http (POST /api/action, /api/active)
          ▼
 AC ──read──▶ control ──Reading──▶ controller.step ──commands──▶ control ──write──▶ AC
                │                                  │
                │                                  ├─ events ──▶ events ring ──▶ store (selected)
                ├─ sample ──▶ history ring         └─ snapshot ──▶ store (on transitions only)
                └─ state snapshot ──▶ http (GET /api/state, /api/history, /api/events)
 config files ◀── store ◀── http (PUT, after core/config.validate) ── new rules apply from the next cycle
```

One control tick: read AC (this read is also the "read before write"), step, write at most one target state,
wait ~5 s, read back. The read-back goes to `step(..., readback=True)`, which only confirms the write or counts a
failed attempt; human changes are detected on poll reads only. A failed attempt is retried on the next poll; a second
one → `write_failed` and `MANUAL` (in `STOPPING` the off write keeps being retried). A poll read showing a state we
didn't set → `MANUAL`; while a write is pending, the unchanged previous state counts as not applied yet (ADR 0003).

## Persistence and flash

| File | Written when | Approx. writes/day |
|---|---|---|
| `profiles.json`, `settings.json` (+ `.bak`) | user saves | ~0 |
| `state.json` (cycle owner, expected AC state, pause, boot counter) | state transitions only | ≤ 50 |
| `events.log` (rotating, 2 × 8 KB; boot counter + uptime, no timestamps) | cycle end, config save, override, takeover, failed write, reboot | ≤ 50 |
| `crash.txt` | before a deliberate reset or after an uncaught exception | ~0 |

History stays in RAM (lost on reboot, accepted). Nothing is written per poll. littlefs spreads wear,
so ~100 small writes/day is negligible for the 2.5 MB file system.

## Secrets and personal settings

The repo holds code and templates only. Every personal value lives on the owner's Mac (gitignored) or on the Pico.

| File | In the repo | On the Mac | On the Pico | Created by |
|---|---|---|---|---|
| `secrets.py` (Wi-Fi SSID/password, `API_TOKEN`, `DAIKIN_IP`) | template `secrets.example.py` | yes, gitignored | yes | owner copies the template; `deploy.py` uploads it |
| `settings.json`, `profiles.json` | no (defaults are in `core/config.py`) | optional backup in `local/` (gitignored) | yes | the Pico on first save from the web UI |
| `state.json`, `events.log`, `crash.txt` | no | no | yes | the Pico |

- Needed before the web UI works → `secrets.py`. Everything else → `settings.json`, edited in the UI
  (`ac_ip` starts as `DAIKIN_IP`).
- `tools/deploy.py` copies code and `secrets.py`; it refuses to run without `secrets.py` and never overwrites the
  JSON files on the Pico. `deploy.py --backup` pulls them into `local/`.
- Missing `secrets.py` on the Pico: no Wi-Fi, a fixed LED error pattern and a message on USB serial.
- Mac tools take the AC IP from an argument or the `DAIKIN_IP` env var. Tests and CI never need secrets:
  they use the simulator on `127.0.0.1` and fake values.
- The privacy hooks block committing `secrets.py`, `.env`, `*.local.*` and any value found in `secrets.py`.

## Failure handling

| Failure | Detection | Response |
|---|---|---|
| AC read fails / times out | exception or timeout (3 s) | reading = unreachable; phase timers keep running; event after 3 misses |
| Write fails or not confirmed | read-back ≠ written | retry next tick; state `STOPPING` keeps retrying "off" until a read shows it off |
| Human used remote / app | any read ≠ expected while we own the AC, or AC on while idle | `MANUAL` for `manual_hold_min`, then `TAKEOVER` (fan, off) if still on |
| Wi-Fi down | `net` link check | reconnect with backoff (5 s → 60 s); `machine.reset()` after 15 min down |
| AC unreachable, Wi-Fi up | `control` misses | after 10 min: reconnect Wi-Fi; after 30 min: reset |
| Loop stall / deadlock | heartbeats stale | watchdog reset (~8 s) |
| Uncaught exception | top-level handler | write `crash.txt`, reset |
| Reboot mid-cycle (crash or owner) | `state.json` says we own a cycle | read AC: if it still matches what we set → finish at once (fan phase, then off); else `MANUAL` |
| Pico switched off mid-cycle | none possible | by design: the AC stays under remote control until the Pico is back (ADR 0003) |
| Broken or missing config file | parse or validation error at boot | use `.bak`, else built-in defaults; warning in `/api/state` |
| Low memory | free heap or largest block below a threshold | event + `gc.collect()`; reset if it stays low (soak test watches the trend) |

## Where code runs

| Code | MicroPython Unix port (Mac, CI) | Pico 2 W | CPython (Mac, CI) |
|---|---|---|---|
| `core/*` | yes | yes | — |
| `shell/*` with `board/host.py` | yes (dev server against the simulator) | — | — |
| `shell/*` with `board/pico.py` | — | yes | — |
| `sim/` (fake Daikin + room model) | yes | — | — |
| `tests/` | yes | `tests/device/` via `mpremote` | — |
| `tools/`, ruff | — | `daikin_check.py` only | yes |

Product code targets MicroPython only ([ADR 0005](decisions/0005-testing.md)): in the core, time comes only from the
injected clock, `ticks_diff` is used for every monotonic comparison (ticks wrap after ~12 days), and temperatures are
integer tenths (the Unix port has double floats, the Pico single).
Code is deployed as `.mpy` (compiled by `mpy-cross` from the same release) to save RAM.

Every source file we write (`.py` in `src/`, `sim/`, `tests/`, `tools/`; `.js`, `.css`, `.html` in `web/`) starts with
`SPDX-License-Identifier: GPL-3.0-only` in its comment syntax, after a shebang if there is one. Vendored files
(`src/lib/`, `tests/lib/`) keep their own headers. Data files (JSON) and docs carry none. CI fails on a missing header.

## Repo layout

```
src/                     copied to the Pico root
  main.py                entry: build Board, start app
  smartac/core/          pure logic (above)
  smartac/shell/         async adapters
  smartac/board/         hardware interface + implementations
  lib/microdot/          vendored, pinned (docs/firmware.md)
  www/                   built, gzipped UI assets
web/                     UI sources (index.html, app.js, style.css)
sim/                     fake Daikin + room model (MicroPython, Unix port): clock, room, device, server, run.py
tests/run.py             test runner (Unix port)
tests/lib/               vendored micropython-lib unittest
tests/unit/              core tests, unittest style
tests/golden/            JSON fixtures: config + sensor trace → expected commands
tests/integration/       full app against the simulator
tests/device/            scripts run on the Pico with mpremote
tools/                   daikin_status.py, daikin_check.py, build_web.py, deploy.py
local/                   owner's backups of settings/profiles (gitignored)
docs/
```

## Testing

([ADR 0005](decisions/0005-testing.md))

| Level | Runs on | What | When |
|---|---|---|---|
| Unit | Unix port | codec, validation, rules, controller transitions, history | every commit (CI) |
| Golden traces | Unix port | config + minute-by-minute readings (incl. remote changes, takeover, gaps, reboots, ticks wrap) → exact commands | every commit (CI) |
| Simulator | Unix port: app + fake Daikin server in one process | whole app with an accelerated clock: cycles, overrides, unreachable AC, HTTP API | every commit (CI) |
| Real AC from the Mac | Unix port | supervised short-timer runs; each write approved, state read back | M4 |
| Device | Pico | Wi-Fi drop, power cycle mid-cycle, watchdog, heap trend, flash files | per milestone; soak before v1.0 |

## Risks to watch

- **Sensors while the unit is off** may be stale or biased (sensor near the ceiling, no airflow). If readings don't
  move when off, triggers never fire. Spike in M0; an external sensor (v1.x) is the fix.
- **Blocking DNS**: `getaddrinfo` blocks the whole loop and can outlast the watchdog. Nothing resolves names:
  the AC is reached by IP and there is no NTP.
- **Large JSON**: the full `dgc_status` response, `profiles.json` PUT and `/api/history` are the biggest allocations.
  Query only needed nodes if the adapter allows it, cap request bodies (16 KB), stream responses.
- **Wi-Fi driver silence after hours** (known upstream reports): covered by the `net` supervisor and resets;
  counters in `/api/state` show how often it happens.
- **Two controllers at once** (Mac runner and Pico during development) will fight. Only one runs at a time.
