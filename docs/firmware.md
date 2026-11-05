# Firmware and pinned versions

What runs on the Pico, and exactly which versions. Change this file whenever the firmware or a vendored
library changes. Why MicroPython: [decisions/0001-language.md](decisions/0001-language.md).

## MicroPython

| | |
|---|---|
| Source | official release ([download page](https://micropython.org/download/RPI_PICO2_W/), [release notes](https://github.com/micropython/micropython/releases/tag/v1.29.0)) |
| Version | v1.29.0 (2026-08-24), tag commit `0fd6c573ea815774668bbb16b8e197c8822368b2` |
| Variant | ARM build (`RPI_PICO2_W`), not RISC-V, not a preview |
| File | `RPI_PICO2_W-20260824-v1.29.0.uf2` (1 774 080 bytes) |
| SHA-256 | `c49c7ed93eb7eeaaa78436b17bd4329e5d4ccbaebc1e79503ad75e0a29c4530e` |
| Flash layout | 1.5 MB firmware, 2.5 MB littlefs |
| Tested on the device | 2026-10-16, Pico 2 W: flashed from the UF2 above (SHA-256 checked), spike S7 passed; 2026-10-17 M1 step 0 and the observer; see below |

C components inside this release (submodule commits at the v1.29.0 tag), for bug reports and forks:

| Component | Repo | Commit |
|---|---|---|
| Wi-Fi driver | georgerobotics/cyw43-driver | `055d64274b014dd7b1c2fc94d26e8a18face7124` |
| TCP/IP | lwip-tcpip/lwip | `77dcd25a72509eb83f72b033d219b1d40cd8eb95` |
| pico-sdk (2.3.0) | raspberrypi/pico-sdk | `98a542c1a62fb549ffb5d66a3e5892b06276b670` |
| TLS | ARMmbed/mbedtls (3.6.6) | `0bebf8b8c7f07abe3571ded48a11aa907a1ffb20` |
| micropython-lib | micropython/micropython-lib | `ee4bb8ff139e24c42b739935fbd8ec7c4d061e02` |

## Vendored libraries

Official releases only, copied into the repo unchanged, so a build never depends on the network.

| Library | Version | Commit | Status |
|---|---|---|---|
| [Microdot](https://github.com/miguelgrinberg/microdot) | v2.7.0 | `7742db9ff9f49635de3387145fafe56bf2377a97` | vendored in `src/lib/microdot/` (core only: `__init__.py`, `microdot.py`, `helpers.py`, LICENSE); deployed as `.mpy` |
| micropython-lib `unittest` (tests only) | 0.11.0, from the micropython-lib commit above | `ee4bb8ff139e24c42b739935fbd8ec7c4d061e02` | vendored in `tests/lib/unittest/` |

Tests run on the MicroPython Unix port built from the same v1.29.0 tag, on the Mac and in CI.
`tools/build_micropython.sh` (`make micropython`) clones the tag into `~/.cache/smartac/` (override with `MPY_DIR`),
checks the tag commit above and builds `ports/unix` with `MICROPY_PY_FFI=0`, the same flags everywhere.
The same build provides `mpy-cross` (`mpy-cross/build/mpy-cross`, emitting mpy v6.3), which `tools/deploy.py` uses.

Host tools, not on the device: `mpremote` 1.29.0 (pinned in `tools/deploy.py`, run through `pipx run`).

## Tested on the device

Spike S7, 2026-10-16, Pico 2 W on USB, v1.29.0 flashed from the checked UF2. AC reads only (op 2).

| Check | Result |
|---|---|
| Firmware | `sys.implementation` 1.29.0, build `RPI_PICO2_W`; `os.uname()` `v1.29.0 on 2026-08-24 (GNU 16.1.0 MinSizeRel)` |
| `tools/daikin_check.py` | OK: HTTP 200, adapter fw `3_6_0`, unit type `RA`, `comm_err 0` |
| Wi-Fi | connect from cold 3.5 s, RSSI -32 dBm, default power mode `0xa11142` (`PM_PERFORMANCE`) |
| GC heap | 446 656 total; free 437 KB at boot and **437 KB after Wi-Fi up** (the driver's buffers are outside the GC heap) |
| asyncio POST to the AC | raw `asyncio.open_connection` + `wait_for_ms`: full `dgc_status` (9 685 bytes) in 0.5–0.6 s, one outlier 3.9 s |
| JSON parse | `json.loads` of 9 685 bytes: 70 ms, holds ~53 KB while the parsed doc is alive |
| Timeout | POST to an unused LAN address with a 3 s timeout raises `TimeoutError` after 3 001 ms; a 50 ms ticker task kept running (61 ticks), so the loop is not blocked |
| Microdot 2.7.0 | import from `.py` 455 ms and ~19 KB heap; hello page 0.24 s per request from the Mac, 404 for unknown routes; 415 KB free while serving; `app.shutdown()` stops cleanly |
| Watchdog | `machine.WDT(timeout=8000)`: fed every 500 ms for 5 s, then starved: reset at ~8 s, `reset_cause()` = 3 (`WDT_RESET`) after reboot. Same with 4 000 ms. Max timeout on RP2350 is **16 777 ms** (`ValueError: timeout exceeds 16777` above; RP2040 was 8 388). |

Notes for the shell:
- The board keeps Wi-Fi up across a soft reset (`mpremote run` after a previous run connects in 0 ms); measure from a hard reset.
- Precompiling with `mpy-cross` cuts the import time, not the heap: see M1 below.
- The AC's address came from a DHCP lease and changed once; the AC needs a DHCP reservation on the router. Without
  one, the observer shows the AC as unreachable and doesn't reset in a loop (architecture.md, "Failure handling").

M1 step 0, 2026-10-17, same board and firmware: `tests/device/probe_m1.py` (no AC traffic).

| Check | Result |
|---|---|
| `reset_cause()` after `machine.reset()` | 3 (`WDT_RESET`), the same as a watchdog reset. Constants on this port: `PWRON_RESET` = 1, `WDT_RESET` = 3. So deliberate resets leave `reset_reason.txt` (board/pico.py). |
| Largest free block | no API (`gc`, `sys`). A binary-search `bytearray` probe takes 115–135 ms (it zero-fills every try): too slow. `micropython.mem_info()` prints `max free sz` (16-byte blocks); copied through `os.dupterm(stream, 0)` it costs **11 ms** and matched the probe exactly (19 389 × 16 = 310 224 bytes on a fragmented heap). Used every 60 s; the text also goes to the USB serial. |
| `wlan.config(pm=PM_NONE)` | accepted (`0x10`, from the default `0xa11142`), still `0x10` after connecting; connect 3.4–3.5 s; `status('rssi')` returns an int (-32/-33 dBm) |
| Microdot `.mpy` against `.py` | each measured as the first import in a fresh interpreter (the qstrs a first import interns stay on the heap and skew a second): `.mpy` **80 ms**, 28 KB allocated, 21 KB held; `.py` 475 ms, 55 KB allocated, 21 KB held. Bytecode is loaded into RAM either way, so the held heap is the same; `.mpy` saves the time and the compile peak. `microdot.mpy` is 12 KB (the `.py` 61 KB). |
| `os.rename` onto an existing file | replaces it on littlefs (`FileStore` relies on this) |

M1 app on the device, 2026-10-17, deployed with `tools/deploy.py` (all `.mpy`, 23 modules, 124 KB):

| Check | Result |
|---|---|
| Import and boot | importing the app (board, tasks, app, store and what they pull in) 369 ms, holds 78 KB; the observe-mode `App` (history arrays included) holds 16 KB more. 346 KB free, largest block 285 KB. |
| Running | after 1 min: 341 KB free, largest block 222 KB, RSSI -39 dBm. `/api/state` OK, `/api/history` (1 440 entries, 43 KB, streamed) in 0.57 s from the Mac, the page in 0.06 s. Mode changes made with the remote show up on the next read. |
| `tests/device/crash_check.py` | PASS: the traceback is in `crash.txt`, the next boot's cause is `crash`, `last_crash` is the exception line |
| `tests/device/wdt_check.py` | PASS: control heartbeat stalled, reset ~17 s later (10 s to go stale + 8 s watchdog), next boot's cause `watchdog` |
| Wi-Fi drop (router restart) | moved to M4, where the owner is present |
| Day soak, 2026-10-17/18 | PASS: 20 h on one boot, no reset or crash, 0 Wi-Fi drops or reconnects, the AC reachable throughout, 0 writes. Free heap 340 576–340 592 bytes the whole time. Largest block 205–253 KB (no trend) while `tools/soak_log.py` polled `/api/state` every minute for 7.5 h (452 polls, none failed); back at 253 KB in every hourly `heap` line of the 12 h after polling stopped. RSSI -29 to -37 dBm. The long soak runs in M3. |

M3 shadow run, 2026-10-20 to 2026-10-27, same board and firmware: the M3 build with `shadow=True`, the AC read
every minute, no writes. Data from the daily `tools/shadow_pull.py` pulls.

| Check | Result |
|---|---|
| Long soak | PASS: 161.8 h (6.7 days, 7 nights) on one boot, no reset or crash; the earlier boots of the run were deploys. 0 writes, 0 `blocked`, 0 `low_memory`. |
| Heap | Free heap in the hourly `heap` lines fell slowly and steadily, 332 864 → 323 120 bytes (about 1.4 KB a day); lowest free 287 536. Largest block 46–239 KB, no trend. Not a risk at this rate (months to run out), but not flat either: to be found before or during the M7 14-day soak. |
| AC reads | 9 715 reads, 16 read errors, all isolated single minutes; the AC was never reported unreachable. |
| Wi-Fi | 1 drop, reconnected on its own; RSSI about -35 dBm. |

M7 device checks, 2026-10-29, same board and firmware, live build (writes on), Day/Night time stages.

| Check | Result |
|---|---|
| Heap after profile edits, before the fix | An evening of profile saves (10 on one boot) fragmented the heap: the largest block in the hourly `heap` lines fell to 28 736 bytes, and the 60 s check went under `LOW_BLOCK` (24 KB) and logged `low_memory` (RAM only then; it goes to flash now). Free heap stayed near 298 KB: room enough, but in small pieces. |
| Heap after profile edits, after the fix | `gc.collect()` after each request, `json.dump` streamed to the file, `json.loads` on the body's bytes. 8 profile saves in 13 min: largest block 132 544–132 752 bytes, free 266 576 at the lowest, 0 `low_memory`. |
| Heap while serving the History tab | 75 rounds of `/api/history` (43 KB, 0.55 s) and `/api/log?tail=300` (32 KB, 0.50 s) from the Mac, back to back, with `/api/state` in between: all 200, free heap 265 872–266 560, largest block 80–82 KB, 0 `low_memory`. The largest block had fallen from 132 KB to 82 KB over the first 2.4 h of the boot before the test, not during it; to watch in the soak. |

M7 soak, 2026-10-29 to 2026-11-05, same board and firmware, live build. Ended early by the owner's decision (planned 14
days). The build predates the web UI changes merged on 2026-11-03. Events cover the last 4.5 days; counters cover the whole run.

| Check | Result |
|---|---|
| Uptime | PASS: 166 h (6.9 days, 7 nights) on one boot, no reset or crash, 0 Wi-Fi drops. |
| AC control | 419 writes, 0 write errors, 0 `blocked`, 0 misses. 10 427 reads, 19 read errors. 91 cycles ended normally, 25–72 min each, none stuck. |
| Heap | Free 289 360–291 808 bytes in the hourly `heap` lines over 4.5 days (290 832 → 289 440, about 0.3 KB a day); largest block 46 960–59 376 bytes, no trend; 0 `low_memory`. |
| Overrides | Not tested: no `manual` or `takeover` event in the 4.5 days of events, or in the earlier pulls of the live builds. 3 pauses. |

## Flashing

1. Download the UF2 above and check it: `shasum -a 256 RPI_PICO2_W-20260824-v1.29.0.uf2`.
2. Hold BOOTSEL while plugging in USB; the Pico appears as a drive (`RP2350`).
3. Copy the UF2 onto the drive. The Pico reboots into MicroPython.
4. Check: `pipx run mpremote==1.29.0 exec "import sys; print(sys.implementation)"` should report v1.29.0.

Flashing MicroPython leaves the littlefs file system (our code and config) in place.

## Upgrading

Only on purpose, never automatically:
1. Read the release notes for the rp2 port, the Wi-Fi driver and lwIP.
2. Flash the new UF2 on the device, run the tests and a soak of at least a few days.
3. Update this file (version, file, SHA-256, submodule commits, test result).

## Patched builds

Used only for a confirmed upstream bug that blocks the project. The fix is made in a fork of the repo that
contains the bug (MicroPython, or a component above), cherry-picked onto the pinned release, tagged
`v<release>-smartac.<n>`, built and flashed over USB. An upstream PR is opened for the same fix.
Go back to the official release once a release includes the fix.

| Build | Based on | Fix | Upstream issue / PR |
|---|---|---|---|
| none | | | |
