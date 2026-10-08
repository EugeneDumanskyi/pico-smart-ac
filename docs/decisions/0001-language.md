# 0001: Firmware language is MicroPython

- Status: accepted
- Date: 2026-10-08

## Context

The Pico 2 W runs 24/7 for years, controls a real AC, and serves the web UI and HTTP API from
[idea.md](../idea.md). The repo is public, and one maintainer owns it. The maintainer is strong in Python and
owns architecture, code review and testing on real hardware, so the code must be easy to read, review
and debug for a Python developer. Raw development speed matters less.

Candidates: MicroPython, C (pico-sdk), Rust (Embassy + `cyw43`). State of each as of October 2026:

| | MicroPython | C (pico-sdk) | Rust (Embassy) |
|---|---|---|---|
| Release | v1.29.0 (2026-08), pico-sdk 2.3.0 | 2.3.1 (2026-09) | embassy-rp 0.10 / cyw43 0.7 (2026-03), no release since |
| Wi-Fi driver | official C `cyw43-driver` | same C driver | separate Rust driver |
| HTTP server | Microdot (async, maintained) | lwIP httpd: static files and SSI/CGI, poor fit for a JSON API | picoserve |
| JSON | built in | cJSON / jsmn, by hand | serde-json-core or serde_json + allocator |
| Config storage | littlefs, files with rename | raw flash sectors | sequential-storage (key-value) |
| Firmware A/B OTA | no (app files only) | yes, RP2350 bootrom example | yes, embassy-boot-rp |
| Test logic on the Mac | CPython + MicroPython Unix port | host build + C test framework | `cargo test` on a `no_std` crate |
| Edit-to-run on device | seconds, no build | build + flash | build + flash |
| Contributor setup | flash a UF2, copy files | arm-gcc, cmake, SDK | rustup target, cargo |

Things that shaped the choice:
- **Wi-Fi is the weakest link in every stack.** The C driver has open reports of the link going silent
  after hours. The Rust driver has more: deadlocks after minutes to hours on the Pico 2 W, and link state
  that stays "up" after a drop. Recovery logic is needed whatever the language.
- **The load is tiny**: one AC poll per minute, a 1440-sample ring buffer, occasional config saves.
  Rust's speed and static memory don't buy much here, and GC pressure stays low if allocation is kept under control.
- **Most of the code handles untrusted input**: HTTP, JSON, and config validation with per-field error paths.
  In C that is where memory bugs hide. In Python it is short and easy to review.
- **Embassy is pre-1.0** and breaks its API between releases; async embedded Rust is also the hardest
  Rust to review for someone new to the language.
- Developing in Rust or C without a debug probe is painful. MicroPython needs only USB (REPL, tracebacks).

## Decision

Write the firmware in **MicroPython**, on the official release pinned in [firmware.md](../firmware.md)
(ARM build, not RISC-V, not previews). Libraries (Microdot, micropython-lib) are official releases,
vendored at pinned versions. Upgrades happen on purpose: flash, test on the device, update `firmware.md`.

If a confirmed upstream bug blocks the project, fix it upstream (a fork with the fix cherry-picked onto the
pinned release, plus a PR), and return to the official release once a release includes the fix.

## Consequences

### Code structure
- **Logic core**: state machine, rule evaluation, config validation. No I/O and no direct time calls
  (the clock is passed in), so the same code runs on CPython, the MicroPython Unix port and the Pico.
- **Adapters**: async Daikin client, storage, HTTP (Microdot), Wi-Fi/NTP supervisor.
  Everything is async; no blocking call (such as `requests`) may stall the web UI or the watchdog.
- **Contract**: the JSON files and HTTP API in [idea.md](../idea.md), plus golden test fixtures
  (config + sensor trace → expected AC writes) that any future implementation must pass.
- Code is written in the subset common to CPython and MicroPython, and CI runs the tests on both.

### Reliability
- Hardware watchdog, fed only from the main loop when it is healthy (never from a timer).
  The rp2 limit is about 8 s, so every network call has a timeout well below that.
- Wi-Fi supervisor: power-save off, reconnect on failure, reboot if the AC has been unreachable for N minutes.
- Allocation discipline: preallocated `array` buffers for history, streamed JSON responses,
  `gc.collect()` each loop. `/api/state` reports free heap and the largest free block, so slow leaks show up.
- Top-level `try/except` that writes the last exception and the reset reason to a small file on flash, then resets.
  Without a debug probe, this is the only post-mortem after a watchdog reset.
- Boot-time recovery from saved state as described in [idea.md](../idea.md#safety).
- A multi-week soak test with the heap and reconnect counters watched, before the device is trusted.

### Updates without a USB cable
- Our code (`.py`/`.mpy`) and the web assets update over Wi-Fi, file-level A/B:
  a minimal `boot.py`, never updated remotely, starts `app_a/` or `app_b/`.
  A new version must report healthy within a few minutes, or the next boot falls back to the previous one.
  Holding BOOTSEL at power-up skips the app and leaves a plain REPL.
- Uploads need the API token and are checked against a SHA-256 manifest before switching.
- Profiles and settings are edited from the web UI and never need an update.
- **MicroPython itself (the UF2) is updated over USB only.** Upgrades are rare by policy, and a patched
  build (an upstream fix, see [firmware.md](../firmware.md)) needs a USB flash anyway, so this is acceptable.

### Testing
- Logic: pytest on CPython and the same tests on the MicroPython Unix port.
- Simulator on the Mac: fake Daikin HTTP server + a simple room model, with an accelerated clock.
- Against the real AC from the Mac: the same logic core with short timers, before it runs on the Pico.

### Accepted downsides
- No firmware-level A/B update or rollback for MicroPython itself.
- GC and fragmentation have to be managed by convention and watched in the soak test, not ruled out by design.
- Bugs in the C layers underneath (`cyw43-driver`, lwIP) are outside our code; we work around them or fix them upstream.

## Later changes

- 2026-10-08: file-level OTA updates move to v1.1 ([roadmap](../roadmap.md)); v1.0 is updated over USB.
  NTP is dropped ([ADR 0004](0004-time.md)), so the Wi-Fi supervisor handles only the link.
- 2026-10-08: tests run only on the MicroPython Unix port, not on CPython, and use `unittest`, not pytest
  ([ADR 0005](0005-testing.md)). Code no longer has to stay in the CPython/MicroPython common subset.
