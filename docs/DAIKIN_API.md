# Daikin local API notes

Unit tested: room AC (type `RA`), Wi-Fi adapter fw 3.6.0.
No auth. Endpoint: `POST http://<ac-ip>/dsiot/multireq`, JSON body.

Read:  `{"requests":[{"op":2,"to":"/dsiot/edge/adr_0100.dgc_status?filter=pv,pt,md"}]}`
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

Paths below are relative to `/dsiot/edge/adr_0100.dgc_status/dgc_status/e_1002` (indoor)
or `/dsiot/edge/adr_0200.dgc_status/dgc_status/e_1003` (outdoor).
Multi-byte numbers are little-endian hex.

## Confirmed on this unit

| Field | Path | Encoding | Source |
|---|---|---|---|
| Power | e_1002/e_A002/p_01 | 00 off, 01 on | pydaikin + observed |
| Mode | e_1002/e_3001/p_01 | 0000 fan, 0100 heat, 0200 cool, 0300 auto, 0500 dry | pydaikin, daikin-ts |
| Fan speed | e_1002/e_3001/p_09 cool, p_0A heat, p_26 auto, p_27 dry, p_28 fan | 0A00 auto, 0B00 quiet, 0300..0700 = 1..5 | pydaikin, daikin-ts |
| Room temp | e_1002/e_A00B/p_01 | int °C | observed |
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

## IR remote caveat

The remote is one-way IR: it does not see API changes and keeps showing its own last state.
Any button press re-sends the remote's full stored state (mode, temp, fan...), overwriting what the API set.
Automation must detect this as a manual override (see docs/idea.md).

## How to map a new field

Snapshot `dgc_status` for adr_0100 and adr_0200, change one setting on the remote, snapshot again, diff.
Ignore drifting sensors/counters (e_A00B/p_05-06, e_2015, e_2028, e_2029, e_A006).
