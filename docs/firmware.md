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
| Tested on the device | 2026-10-16, Pico 2 W: flashed from the UF2 above (SHA-256 checked), spike S7 passed; see below |

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
| [Microdot](https://github.com/miguelgrinberg/microdot) | v2.7.0 | `7742db9ff9f49635de3387145fafe56bf2377a97` | vendored in `src/lib/microdot/` (core only: `__init__.py`, `microdot.py`, `helpers.py`, LICENSE) |
| micropython-lib `unittest` (tests only) | 0.11.0, from the micropython-lib commit above | `ee4bb8ff139e24c42b739935fbd8ec7c4d061e02` | vendored in `tests/lib/unittest/` |

Tests run on the MicroPython Unix port built from the same v1.29.0 tag, on the Mac and in CI.
`tools/build_micropython.sh` (`make micropython`) clones the tag into `~/.cache/smartac/` (override with `MPY_DIR`),
checks the tag commit above and builds `ports/unix` with `MICROPY_PY_FFI=0`, the same flags everywhere.

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
- Precompiling Microdot with `mpy-cross` should cut the import time and heap; not tried yet.
- The AC's address came from a DHCP lease and changed once; the AC needs a DHCP reservation on the router.

## Flashing

1. Download the UF2 above and check it: `shasum -a 256 RPI_PICO2_W-20260824-v1.29.0.uf2`.
2. Hold BOOTSEL while plugging in USB; the Pico appears as a drive (`RP2350`).
3. Copy the UF2 onto the drive. The Pico reboots into MicroPython.
4. Check: `mpremote exec "import sys; print(sys.implementation)"` should report v1.29.0.

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
