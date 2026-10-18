# Roadmap

The one plan for the project, from now to v1.0 and beyond. Design: [idea.md](idea.md).
Structure: [architecture.md](architecture.md). Each milestone ends with something that runs.

Rules for every milestone that touches the real AC: each write is approved by the owner first, the state is read back
afterwards, and the AC is left as it was found. Mold-proof is never switched on.

## Overview

| # | Milestone | Ends with | Pico | Real AC |
|---|---|---|---|---|
| M0 | Spikes and skeleton | CI green on the Unix port; open hardware questions answered | yes | read + approved writes |
| M1 | Pico observer | Pico reads the AC every minute and serves `/api/state`, `/api/history`, a bare status page | yes | read only |
| M2 | Core logic on the host | controller runs on the Mac against the simulator; golden traces pass on the Unix port | no | no |
| M3 | Shadow mode | Pico runs the real controller on real data and logs what it *would* do | yes | read only |
| M4 | Closed loop, supervised | automation controls the real AC; all safety cases pass | yes | writes |
| M5 | Control API and Status UI | everything operable from a phone | yes | writes |
| M6 | Profile and settings editor | profiles edited and validated from the phone | no (host), then yes | no |
| M7 | v1.0 | history chart, docs, soak, tagged release | yes | writes |

M1 and M2 can run in parallel. **Start with M0.**

## Decision log

From the design review, 2026-10-08.

| # | Decision | Where |
|---|---|---|
| D1 | The AC is off by default and owned by the Pico while it runs; a cycle always ends with off. | [idea.md](idea.md#how-it-is-used) |
| D2 | An AC we didn't start gets a manual hold, then a takeover (fan, then off) if it's still on. | [ADR 0003](decisions/0003-ac-ownership.md) |
| D3 | No backstop when the owner switches the Pico off mid-cycle: the AC is then under remote control. The AC's off-timer is not used. | [ADR 0003](decisions/0003-ac-ownership.md) |
| D4 | Relative time only: no NTP, calendar or time windows; profiles chosen by hand. | [ADR 0004](decisions/0004-time.md) |
| D5 | Pausing or switching to `off` during a cycle offers *finish* or *leave running*. | [ADR 0003](decisions/0003-ac-ownership.md) |
| D6 | `use` rule pointers dropped; profiles list their own rules. | [idea.md](idea.md#profilesjson) |
| D7 | File-level OTA updates move to v1.1; v1.0 is updated over USB. | [ADR 0001](decisions/0001-language.md) |

## M0: Spikes and skeleton

| | |
|---|---|
| Goal | Answer every hardware question that could change the design, and have a working repo and CI. |
| Scope | Repo layout from architecture.md; CI (ruff, SPDX header check, Unix port built from v1.29.0, one trivial core test); Unix port build on the Mac. Spikes below. |
| Out of scope | Any product code beyond the skeleton. |
| Deliverables | CI pipeline; spike results in `DAIKIN_API.md`; `firmware.md` "Tested on the device" filled in with free heap. |
| Done when | CI green; `tests/run.py` passes on the Mac; each spike has a recorded result (works / doesn't / workaround). |
| Dependencies | none |
| Risks | Readings don't update while the AC is off (would push the external sensor into v1.0). |
| Needs | Pico; real AC (read, plus approved writes). |

Spikes:

| Spike | Where | Question |
|---|---|---|
| S1 Readings while off | Mac, read only, overnight | Do room temp/humidity update while the unit is off? How much do they jump when it starts/stops? |
| S2 Dry mode | real AC, approved write | Does `0500` work; does dry take a target temp; fan speed in dry? |
| S3 Fan speed | real AC, approved write | Does a real fan-speed change stick, per mode field? |
| S4 Streamer | real AC, approved writes | Does streamer survive mode changes and off/on done via the API? |
| S5 Write behaviour | real AC | One request for power + mode + target + fan, or a sequence? Time until a read reflects a write? Writes while off? |
| S6 Response size | Mac, read only | Size of `dgc_status`; can narrower paths or filters be queried? |
| S7 Pico runtime | Pico | Flash v1.29.0 (check SHA-256); free heap after Wi-Fi up; asyncio POST to the AC with timeout; Microdot hello; watchdog. |

First tasks, in order:
1. Create the skeleton (`src/smartac/{core,shell,board}`, `tests/`, `sim/`, `web/`) and the CI workflow with one core test on the Unix port.
2. Flash v1.29.0, verify the SHA-256, run `tools/daikin_check.py`, record free heap in `firmware.md` (S7 start).
3. Start S1: a read-only logger on the Mac (one reading per minute to CSV) while the AC stays off overnight.
4. Prepare S2–S5 as a short scripted session; ask the owner to approve each write; read back; restore the AC.

## M1: Pico observer

| | |
|---|---|
| Goal | The Pico runs for days next to the AC without crashing or leaking, reading only. |
| Scope | `board/pico.py`, `shell/net.py` (Wi-Fi supervisor), watchdog task, crash log, `core/daikin_codec.py` (read), `shell/daikin.py`, `core/history.py`, Microdot with `GET /api/state`, `/api/history`, a bare HTML status page. |
| Out of scope | Writes to the AC, rules, config files, auth. |
| Deliverables | Deployable app (`tools/deploy.py`, `.mpy` build); device test scripts. |
| Done when | Day soak (12–14 h with `tools/soak_log.py`): no unexplained reset; heap free and largest block flat after the first hour; reconnect and reset counters visible in `/api/state`. |
| Dependencies | M0 (S6, S7). |
| Risks | Wi-Fi driver silence after hours; fragmentation from large responses. |
| Needs | Pico; real AC read only. |

M2 finished first, so M1 puts the whole app on the Pico in **observe mode**: `App(observe=True)` builds no controller,
and a tick only reads the AC and records history, so the write path can't be reached. M3 turns the controller on with
`dry_run`. Done so far (2026-10-17): step 0 device checks ([firmware.md](firmware.md#tested-on-the-device)),
`core/history.py`, `FileStore`, `board/pico.py`, `shell/net.py`, `shell/api.py`, `shell/tasks.py`, the status page,
`tools/deploy.py` and `tools/soak_log.py`; deployed, with the watchdog and crash checks passed on the device.
The day soak passed on 2026-10-18 ([firmware.md](firmware.md#tested-on-the-device)): 20 h on one boot, no reset,
heap flat, 0 writes. **M1 is done.** Moved on 2026-10-17, as the owner can't keep a computer running for 3 days: the multi-day soak
goes to M3, which runs a week anyway and is recorded on the Pico itself (an hourly `heap` line in `events.log`);
the Wi-Fi drop test (router restart) goes to M4's safety checklist, where the owner is present.

## M2: Core logic on the host

| | |
|---|---|
| Goal | All decision logic done and tested without hardware. |
| Scope | `core/config.py` (defaults, validation, error paths), `core/rules.py`, `core/controller.py` (all states incl. `STOPPING` and `TAKEOVER`, ADR 0003/0004), events; the codec's write side (pulled forward from M4: the simulator parses the same writes); `sim/` fake Daikin + room model; golden traces; a runner that drives the app against the simulator. |
| Out of scope | Pico, HTTP API beyond what the runner needs, UI. |
| Deliverables | Core modules; ≥ 20 golden traces (each default profile, remote press in every state, AC on at boot, hold then takeover, unreachable AC, reboot mid-cycle, hard cap, config change mid-cycle, pause finish/leave, ticks wrap). |
| Done when | Unit and golden tests pass on the Unix port; a simulated month at 1000× speed runs with no stuck state. |
| Dependencies | M0 skeleton. |
| Risks | Room model too simple to be useful (keep it simple; it tests logic, not comfort). |
| Needs | neither. |

## M3: Shadow mode

| | |
|---|---|
| Goal | Validate triggers and thresholds on real data with zero risk. |
| Scope | M2 core on the Pico, `profiles.json`/`settings.json` from flash (edited by file copy), `dry_run`: commands become events (`would set dry, fan auto`). Brought forward from M5: `POST /api/active` (with the token) and a profile dropdown, `GET /api/events`; new: `GET /api/log` and `tools/shadow_pull.py`. |
| Out of scope | Writes to the AC, editor UI. |
| Deliverables | Shadow event log; `tools/shadow_pull.py`; a short tuning note in `idea.md` (thresholds, confirm reads). |
| Done when | 7 nights of normal use in shadow mode; every "would" decision reviewed against the history and judged sensible; remote use correctly seen as "human". Doubles as the long soak (moved from M1): no unexplained reset, and the hourly `heap` lines in `events.log` flat over the week. |
| Dependencies | M1, M2. |
| Risks | Readings while off (S1) make triggers useless → external sensor moves into v1.0. |
| Needs | Pico; real AC read only. |

**Shadow mode** is `App(shadow=True)` (`main.py`): the controller gets `dry_run` forced on, whatever
`settings.json` says, and so does a cycle restored from `state.json`. A write that still came out of the controller
would not be sent: it becomes a `blocked` event, and `writes` stays 0. Decisions (`cycle_start`, `phase`, `would`,
`hard_cap`, `profile`, `unreachable`, `blocked`) go to `events.log` as well as the RAM ring, each with the room temp and
humidity it was made on, and `phase` and the last `would` say why a phase ended (`goal hum <= 65` or `45 min`). The log
is 2 × 64 KB, at least 9 days.

The week:
1. `make test`, deploy; record the heap after boot and the `.mpy` size in `firmware.md`.
2. Smoke test with the owner: enter the token on the phone, pick `night`; check `profile`, then `cycle_start` and
   `would` when a trigger is met. A remote press while idle gives `manual: AC on, not started by us`, then
   `takeover` and `would set …` once the hold is over. `writes` stays 0.
3. Seven nights: `night` at bedtime, `day`/`away`/`off` by day as the owner likes, the remote used as usual.
   `python3 tools/shadow_pull.py <pico-ip>` each morning; nothing else needs the Mac.
4. Review with `--report`: each `would` against the history; remote use always seen as `manual`; the soak (one boot
   or every reset explained, flat hourly `heap` lines, `writes` 0, no `blocked`). One boot crosses the ticks
   half-period at about 6.2 days: watch the ages and timers there.
5. Tuning note in `idea.md`, the soak result in `firmware.md`, M3 marked done here.

M5 keeps the rest of the control API (`/api/action`, `/api/ac/test`) and the full Status tab.

## M4: Closed loop, supervised

| | |
|---|---|
| Goal | Automation controls the real AC safely. |
| Scope | Write path in the client (the codec's write side is done in M2 and tested against the simulator only), read-back, `MANUAL`, `TAKEOVER`, `STOPPING`, boot recovery. First from the Mac (Unix port, short timers), then on the Pico. |
| Out of scope | UI actions (M5). |
| Deliverables | Safety checklist with results: remote press in each phase; Daikin app change; AC on at boot → hold → takeover; Pico switched off mid-cycle and back on (AC untouched → finished; AC changed → hold); Wi-Fi off mid-cycle; router restart while idle (moved from M1: reconnect without a reset under 15 min; a new AC IP → one reset, then "unreachable", no loop); AC unreachable at the end of a phase; hard cap hit; config saved mid-cycle. |
| Done when | Every checklist case passes on the real AC, and the AC ends in the expected state each time. |
| Dependencies | M3. |
| Risks | Rejected writes misread as overrides; two controllers running at once during testing. |
| Needs | Pico; real AC writes (owner present). |

## M5: Control API and Status UI

| | |
|---|---|
| Goal | Day-to-day use from a phone. |
| Scope | `/api/action` (incl. finish/leave choice), `/api/ac/test`; the finish/leave choice for `POST /api/active` in the UI (M3 brought the route, the token check, `GET /api/events` and a bare events list forward); Status tab; LED status. |
| Out of scope | Profile editor, history chart. |
| Deliverables | `web/` UI (vanilla JS), `tools/build_web.py`, API tests against the simulator. |
| Done when | Every Status-tab button works on a phone against the Pico; requests without a token are refused; UI ≤ 60 KB gzipped. |
| Dependencies | M4. |
| Risks | RAM spikes while serving assets (stream files). |
| Needs | Pico; real AC for acceptance. |

## M6: Profile and settings editor

| | |
|---|---|
| Goal | Profiles and settings are edited, validated and saved from the phone. |
| Scope | `GET/PUT /api/profiles`, `/validate`, `/reset`, `GET/PUT /api/settings`; Profiles tab (forms, plain-language summary, field-level errors, copy rule); Settings tab; device info. |
| Out of scope | Profile import/export, multiple users. |
| Deliverables | Editor UI; validation tests for every rule in idea.md; atomic save with `.bak`. |
| Done when | Every validation error highlights its field; a power cut during save leaves a valid config; a saved change applies from the next cycle only. |
| Dependencies | M5. |
| Risks | Editor size and complexity (keep one form component per level). |
| Needs | host + simulator; Pico for acceptance. |

M6 is built on the host first, on the branch `m6-editor`, while M3's week runs on the Pico (deploying reboots it).
Done so far (2026-10-18): a table test with one accepted and one rejected change per row of idea.md's "Validation"
table; the app's save path (`pending`, applied by the next tick; `config_saved` events; shadow mode keeps the
owner's `dry_run` in the file); the routes (idea.md, "HTTP API"); power cuts injected at each point of
`FileStore.save`, each followed by a clean boot on the old or new document; `make dev`; the Profiles and Settings tabs,
checked in headless Chrome at phone width (each kind of error marks its field or block, save, revert, restore
defaults, rename, copy a rule). Page size: about 39 KB before gzip.

Left, after M3's week is reviewed: deploy; heap during a 16 KB PUT and the new sizes in `firmware.md`; the phone
checklist; `tests/device/save_check.py` with about 5 USB pulls mid-save; then merge to `main`.

## M7: v1.0

| | |
|---|---|
| Goal | A release someone else can install and trust. |
| Scope | History tab (SVG chart, no library), events on flash, README with setup, install guide, `firmware.md` complete. |
| Out of scope | Everything in later releases. |
| Deliverables | Tag `v1.0.0`; release notes. |
| Done when | 14 days of continuous uptime (`dry_run` or `off` allowed while nobody is home), including ≥ 7 nights with automation on: no stuck on/off, no override missed or fought, heap flat, all resets explained; fresh install from the README works. |
| Dependencies | M6. |
| Risks | Rare Wi-Fi faults show up only in the long soak. |
| Needs | Pico; real AC. |

## After v1.0

| Release | Goal | Scope | Needs |
|---|---|---|---|
| v1.1 OTA app updates | update code without USB | file-level A/B (`app_a`/`app_b`), SHA-256 manifest, health check and fallback ([ADR 0001](decisions/0001-language.md)) | Pico |
| v1.2 External sensor | accurate comfort control | SHT40/AHT20 at seat height; 0.1 °C hysteresis; dew point / absolute humidity conditions; per-condition sensor source | Pico, sensor |
| v1.3 IR receiver | instant, exact override detection | VS1838B; decode Daikin IR frames; know the human's state; optional IR LED for streamer (never mold-proof without the owner's say) | Pico, IR parts, AC |
| v1.4 MQTT / Home Assistant | phone control and notifications | publish state, accept actions via a LAN broker; HA discovery | broker |
| v1.x later | smarter and cheaper | profile timer ("night for 8 h, then off"), presence (ARP ping, mmWave), night sleep curve, energy per cycle, learn room warm-up, button/display, log replay; opt-in wall clock only if someone needs forecasts or tariffs | varies |
