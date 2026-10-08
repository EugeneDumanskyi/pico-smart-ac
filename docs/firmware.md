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
| Tested on the device | not yet |

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
| [Microdot](https://github.com/miguelgrinberg/microdot) | v2.7.0 | `7742db9ff9f49635de3387145fafe56bf2377a97` | planned, not vendored yet |
| micropython-lib `unittest` (tests only) | 0.11.0, from the micropython-lib commit above | `ee4bb8ff139e24c42b739935fbd8ec7c4d061e02` | vendored in `tests/lib/unittest/` |

Tests run on the MicroPython Unix port built from the same v1.29.0 tag, on the Mac and in CI.
`tools/build_micropython.sh` (`make micropython`) clones the tag into `~/.cache/smartac/` (override with `MPY_DIR`),
checks the tag commit above and builds `ports/unix` with `MICROPY_PY_FFI=0`, the same flags everywhere.

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
