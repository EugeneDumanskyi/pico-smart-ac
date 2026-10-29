# Daikin local API notes

Unit tested: room AC (type `RA`), Wi-Fi adapter fw 3.6.0.
No auth. Endpoint: `POST http://<ac-ip>/dsiot/multireq`, JSON body.

Read:  `{"requests":[{"op":2,"to":"/dsiot/edge/adr_0100.dgc_status?filter=pv"}]}` (the codec reads both units this way)
Write: `op: 3` on the same endpoint. Response `rsc 2004` = accepted, `rsc 4000` = rejected.
```
{"requests":[{"op":3,"to":"/dsiot/edge/adr_0100.dgc_status",
  "pc":{"pn":"dgc_status","pch":[{"pn":"e_1002","pch":[{"pn":"e_A002","pch":[{"pn":"p_01","pv":"01"}]}]}]}}]}
```
Accepted (2004) and verified by reading back, 2026-10-07:
- mode e_3001/p_01: fan `0000` -> cool `0200` (real change)
- cool target e_3001/p_02: 22.5 -> 23.0 °C, value `2E` (= 23 x 2, hex)
- power e_A002/p_01, fan-mode fan speed e_3001/p_28 (written with unchanged values)
Rejected (4000): streamer e_3003/p_20 = 01 while in fan mode. Readable, but not writable this way.

Full cycle from the Pico 2 W (MicroPython v1.29.0, blocking `requests`), 2026-10-08, AC off in fan mode at start:

| Step | Write | Result |
|---|---|---|
| start | power `01` + mode `0200` (cool) in **one** request, unit off | 2004; read 5 s later: on, cool |
| after 10 min | mode `0000` (fan), power unchanged | 2004; read 5 s later: on, fan |
| after 15 min | power `00` | 2004; read 5 s later: off, mode stays fan |

- Power and mode can be set together while the unit is off (spike S5, partly). A read 5 s after a write
  already shows the new state. Target temp, fan speeds and streamer were not written and stayed unchanged.
- Room readings (1-min reads): cool 10 min took 29 °C / 80 % to 27 °C / 70 %. Temperature is whole degrees
  and moves slowly; humidity reacts faster and rose back to 75–80 % in fan mode and after off (coil drying).

## Readings while off (spike S1, 2026-10-16)

Read only, from the Mac (`tools/daikin_log.py`), every 10 s and every 60 s. The unit was switched with the remote:
off 18 min, cool 14 min, fan 20 min, off 24 min. No reference thermometer in the room.

| Phase | Room temp (`p_05` / 2) | Room humidity | Outdoor |
|---|---|---|---|
| off, 18 min | 29.0 flat | flickers 80 ↔ 85 (6 changes) | 29.0 flat |
| cool, 14 min | first step after 80 s, then ~0.25 °C/min: 29.0 → 26.0 | 85 → 75, last 5 min at 75 | 29.0 → 33.5 |
| fan, 20 min | 26.0 → 27.0 (one dip to 25.5) | 75 → 80 within 10 s, 85 after 6 min | 33.5, then 29.5 from 13 min |
| off, 24 min | 27.0 → 27.5 after 7 min → 28.0 after 17 min | 85 flat | 29.5 flat |

- **Readings do update while the unit is off.** After the stop the room temperature kept climbing back towards the
  pre-cool value, and humidity moves while off. There is **no step** at start or stop: start shows the first change
  after 80 s, stop shows none at all. The start/stop jump the design worried about is below the 0.5 °C resolution.
- `e_A00B/p_05` (and `p_06`, identical so far) is room temperature × 2, little-endian: **0.5 °C resolution**.
  `p_01` is `floor(p_05 / 2)`, so `p_01` lags by up to 0.9 °C. They are not drifting counters.
- Humidity has **5 % steps** and flickers between neighbouring steps while the room is steady (6 times in 18 min).
  Rules on humidity need ≥ 5 % hysteresis or a time filter. 85 % was the highest value seen; it may be a ceiling.
- The fan phase after cool brings humidity straight back (75 → 85 % in 6 min): the coil drying effect from idea.md.
- Outdoor temperature is measured at the outdoor unit: it reads 4.5 °C high while the compressor runs and settles
  ~13 min after it stops. While off it barely moved (0.5 °C in 42 min), so it may refresh slowly when idle.
- Overnight on the Pico (`tools/pico_log.py`, 1 reading/min, 2026-10-16 03:23 to 2026-10-17 02:05 UTC): 1366 rows,
  no gap over 90 s, no read errors, with the AC used normally (13 off periods). The longest off period, 6.5 h, went
  25.5 → 29.5 °C in steady 0.5 °C steps over 3 h, then held 29.5 for 3.5 h while outdoor still moved (30.0 → 29.0).
  Every off period shows the room warming back, and no start or stop has a step larger than one 0.5 °C reading.
  **S1 is closed: built-in readings are good enough for v1 triggers; the external sensor stays in v1.2.**

## Dry mode as a write (spike S2, 2026-10-16)

Written from the Mac with `core/daikin_codec.write_request`, each step read back 5 s later. The AC started off in fan.

| Step | Write | Result |
|---|---|---|
| start | power `01` + mode `0500` (dry), no target, no fan | 2004; on, dry, fan auto (`p_27` was already `0A00`) |
| after 5 min | power `01` + mode `0000` (fan) | 2004; on, fan, fan speed 3 (`p_28` kept) |
| after 5 min | power `00` | 2004; off, mode stays fan |

- Dry is writable like cool and fan, and power + dry mode work in one request while off.
- Response body is exactly `{"responses":[{"rsc":2004,"fr":"/dsiot/edge/adr_0100.dgc_status"}]}`, as the codec assumes.
- Switching on also changed status fields in `e_3003`: `p_01` 00 → 02, `p_37` 00 → 02 (back to 00 at off),
  `p_2F` 0400 → 0A00, `p_0C` 37 → 3A. None of the mold-proof candidates (`p_1B`, `p_29`, `p_2C`, `p_30`, `p_36`)
  changed. The dry → fan switch changed only the mode.
- Run fan for a few minutes after cool or dry before switching off, to dry the coil (the cycle in idea.md).

## Fan-speed change (spike S3, 2026-10-16)

Same method as S2, fan mode only (no compressor). The AC started off in fan, speed 3; one minute per step.

| Step | Write | Result |
|---|---|---|
| start | power `01` + mode `0000` | 2004; on, fan, speed 3 |
| speed 1 | power `01` + mode `0000` + `p_28 = 0300` | 2004; speed 1, only `p_28` changed |
| speed 3 | power `01` + mode `0000` + `p_28 = 0500` | 2004; speed 3, only `p_28` changed |
| stop | power `00` | 2004; off, fan, speed 3 |

- A fan-speed change is writable together with power and mode in one request, and is applied while running.
- `e_3003/p_37` follows power (00 off, 02 on) in fan mode too; `p_2F` did not move in fan mode (it did in dry).

## Cool with target and fan in one request (spike S5, 2026-10-16)

Same method. The AC started off in fan, speed 3, with a stored cool target of 22.5 °C (`2D`) and cool fan speed 3.

| Step | Write | Result |
|---|---|---|
| start | power `01` + mode `0200` + `p_02 = 34` (26.0 °C) + `p_09 = 0600` (speed 4) | 2004; on, cool, 26.0, speed 4: exactly those four params changed |
| after 5 min | the same with `p_02 = 2D` (22.5 °C) + `p_09 = 0500` (speed 3) | 2004; target and fan changed, mode and power kept |
| after 4 min | power `01` + mode `0000` + `p_28 = 0700` (speed 5) | 2004; on, fan, speed 5 |
| after 1 min | `p_28 = 0300` (speed 1), then after 1 min `0500` (speed 3) | 2004 each; only `p_28` changed |
| after 5 min | power `00` | 2004; off, fan, speed 3, cool target back at 22.5 |

- Every combination the codec writes is now confirmed: power + mode + cool target + fan in one request, from off
  and while running. A changed target or fan applies without a mode change.
- Room temp 29.5 → 27.5 °C and humidity 85 → 75 % in 9 min of cool; humidity back at 85 % after 5 min of fan.
- The speed 5 → 1 change was clearly felt at the air outlet, not only seen in the read-back.
- Restoring the stored target and fan at the end keeps the API state in line with what the remote last sent.

## Streamer across API writes (spike S4, 2026-10-16)

No extra writes: the streamer was on (set with the remote, `e_3003/p_20 = 01`) through S2, S3 and S5, and every
write there was diffed against a full read. `p_20` never changed across 3 off → on and 3 on → off switches and the
mode changes cool ↔ fan ↔ dry, and still read `01` afterwards. The streamer stays as the remote set it; the codec
never writes it (a direct write is refused, see the top of this file).

## Read size and narrower reads (spike S6, 2026-10-16)

Read only (op 2), measured from the Mac; the Pico gets the same bytes.

| Request | Bytes | Note |
|---|---|---|
| indoor `adr_0100.dgc_status` | 9 685 | same with `?filter=pv,pt,md` (that filter keeps everything) |
| indoor `?filter=pv` | 4 692 | drops the `md` metadata, keeps `pn`, `pt`, `pv`: all 114 values identical to the full read |
| outdoor `adr_0200.dgc_status` | 4 734 | `?filter=pv`: 2 390 |
| indoor + outdoor in one request | 14 404 | both `?filter=pv`: **7 067** |
| `adr_0100.i_power.week_power` | 324 | runtime counters |

- Sub-node paths are not supported: `…dgc_status/dgc_status/e_1002[/e_A00B[/p_01]]`, the outdoor
  `…/e_1003/e_A00D` and a dotted form all return `rsc 4041` (not found) with no `pc`. The smallest read of a
  sensor is the whole unit with `?filter=pv`.
- Other filters: `filter=pn` = full size, `filter=md` = 8 178 indoor; `filter=pv,pn` = `filter=pv`.
- Reply time ~0.2–0.4 s from the Mac; from the Pico 0.5–0.6 s for the full indoor read, one outlier 3.9 s.
- On the Pico, parsing the full indoor reply holds ~53 KB of heap (firmware.md). `?filter=pv` should halve that.
- `dgc_status` also has `last_ope` (`type`, `time`, `chg_flg`), next to `e_1002` and kept by `?filter=pv`. `time` is
  the local time of the last operation with a `Z` suffix (`"2026-10-28T08:49:01Z"`); after an API write it matched the
  write to the second, `type` 100 (99 seen while the unit was off; other values not mapped yet). `core/clock.py` uses
  it as the time of day (ADR 0006).

## Write format used by the codec

`core/daikin_codec.py` (`write_request`) builds one op-3 request to the indoor unit, groups in this order:

| AC state | Params written |
|---|---|
| off | `e_A002/p_01 = 00` only. Mode, target and fan stay as they are (tested 2026-10-08). |
| on (cool, dry, fan) | `e_A002/p_01 = 01`, `e_3001/p_01 = mode`, cool target `e_3001/p_02` if set, the mode's fan param if set |

It never writes `e_3003` (streamer, mold-proof candidates) or anything outdoor, and refuses heat and auto (only the
remote sets those). A write counts as done when every response has `rsc 2004`.

All of these writes are confirmed on the real unit: dry mode `0500` and the write response body in S2, a fan-speed
change in S3, power + mode + cool target + fan in one request in S5 (above).

Paths below are relative to `/dsiot/edge/adr_0100.dgc_status/dgc_status/e_1002` (indoor)
or `/dsiot/edge/adr_0200.dgc_status/dgc_status/e_1003` (outdoor).
Multi-byte numbers are little-endian hex.

## Confirmed on this unit

| Field | Path | Encoding | Source |
|---|---|---|---|
| Power | e_1002/e_A002/p_01 | 00 off, 01 on | pydaikin + observed |
| Mode | e_1002/e_3001/p_01 | 0000 fan, 0100 heat, 0200 cool, 0300 auto, 0500 dry | pydaikin, daikin-ts |
| Fan speed | e_1002/e_3001/p_09 cool, p_0A heat, p_26 auto, p_27 dry, p_28 fan | 0A00 auto, 0B00 quiet, 0300..0700 = 1..5 | pydaikin, daikin-ts |
| Room temp | e_1002/e_A00B/p_01 | int °C, = floor(p_05 / 2) | observed |
| Room temp, 0.5 °C | e_1002/e_A00B/p_05 (p_06 same so far) | int16 / 2 °C | S1 2026-10-16 |
| Room humidity | e_1002/e_A00B/p_02 | int % | observed |
| Outdoor temp | e_1003/e_A00D/p_01 | int16 / 2 °C | pydaikin |
| Target temp | e_1002/e_3001/p_02 cool, p_03 heat, p_1D auto | value / 2 °C | pydaikin |
| **Streamer** | e_1002/e_3003/p_20 | 00 off, 01 on | **own test 2026-10-07** (toggled on remote, diffed) |

## From pydaikin, not yet verified here

| Field | Path | Encoding |
|---|---|---|
| Comfort | e_1002/e_3003/p_1D | 00/01 |
| Econo | e_1002/e_3003/p_24 | 00/01 (not reported by this unit) |
| Powerful | e_1003/e_3002/p_44 | 00/01, auto-cancels ~20 min |
| Outdoor quiet | e_1003/e_3002/p_3D | 00/01 |

## Unknown

- Mold-proof: probably one of the unnamed 00/01 switches in e_3003
  (p_1B, p_29, p_2C, p_30, p_36). Currently off. Find it by toggling and diffing.
- e_3003/p_28 = 01: an on/off switch, not streamer, not mold-proof.
- Dry offset: in dry the remote sets a relative value, 0 to −3.0 in 0.5 steps, not a target temperature.
  Field not mapped; find it by changing it on the remote while in dry and diffing. The codec never writes it.

## IR remote caveat

The remote is one-way IR: it does not see API changes and keeps showing its own last state.
Any button press re-sends the remote's full stored state (mode, temp, fan...), overwriting what the API set.
Automation must detect this as a manual override (see docs/idea.md).

## How to map a new field

Snapshot `dgc_status` for adr_0100 and adr_0200, change one setting on the remote, snapshot again, diff.
Ignore drifting sensors/counters (e_A00B, e_A00D, e_2015, e_2028, e_2029, e_A006).
