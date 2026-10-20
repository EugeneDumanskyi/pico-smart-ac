# M4 safety checklist

Every case the M4 deliverable lists ([roadmap.md](roadmap.md), "M4"), run on the real AC with the owner present.
The host half runs the app from the Mac (`sim/supervised.py`); the Pico half repeats what needs the board's own
hardware once the Pico writes.

## Host runner

```sh
DAIKIN_IP=<ac-ip> make supervised            # a new session in local/m4/<utc-stamp>/ (gitignored)
make supervised M4_DIR=local/m4/<utc-stamp>  # the same session again: a restart mid-cycle
make supervised SIM=1                        # the same against the simulator, for a rehearsal
```

- **Short timers.** `MINUTE_S` (default 15) is the length of the controller's minute, so every minute timer (phase,
  hold, release, cooldown, hard cap, pause) runs 4× faster. `poll_s` (60), the 5 s read-back and the AC stay real.
  The session config is set in real time and converted to controller minutes, so at any `MINUTE_S` (5 to 60) our
  writes are at least 4 real minutes apart (cool → fan → off → the next on), and so are a remote press and our
  takeover. The prompt shows the time since our last write and flags anything shorter.
- **Every write is approved.** The runner prints the tick's reading and the state it wants, then waits at
  `approve? [y/N]`. On `y` it reads the AC again and writes only if nothing changed while the prompt was open.
  Anything else declines: the core sees a rejected write.
- **Session config.** On first start the session directory gets `profiles.json` with one profile, `m4` (active):

  | Rule | Trigger | Phases | Cooldown |
  |---|---|---|---|
  | 0 `trigger` | `temp > 10`, 1 read, at most once per 240 min | cool 26 °C fan auto 16 min → fan 3, 16 min | 16 min |
  | 1 `cap` | never (`temp < 10` is below the sensor limits); started with `run` | cool 26 °C 24 min → fan 3, 24 min (48 in all) | 16 min |

  and `settings.json`: hard cap 48 min, hold 16, release 5, finish fan 16, `poll_s` 60, `dry_run` off. `ac_ip` stays
  empty: the address comes from `DAIKIN_IP`. Times here are controller minutes at the default 15 s (16 = 4 real
  minutes); a one-cycle case takes about 12 real minutes.
- **Files.** The session directory holds what the Pico keeps on flash (`events.log` with every decision, `state.json`,
  the config) and `session.log`: start and end in UTC, the minute length, each approval, action and fault. If the Pico
  runs in shadow mode meanwhile, the M3 review skips these windows; it logs our writes as `manual`, a free
  cross-check.
- **Controls**, from a second terminal (token `dev`):

  ```sh
  run()  { curl -s -X POST -H 'X-Token: dev' -d "$2" http://127.0.0.1:8080$1; echo; }
  run /run/action '{"action": "run", "profile": "m4", "rule": 0}'  # also stop, stop_now, pause, resume, clear_manual,
                                                                  # set_profile
  run /run/fault '{"read": "unreachable"}'                        # reads fail until {"read": "ok"}
  run /run/sim '{"remote": {"power": true, "mode": "cool", "target_c": 24, "fan": "auto"}}'  # SIM=1 only: a remote
  run /run/sim '{"unreachable": 3}'                               # SIM=1 only: the fake AC silent for 3 real minutes
  ```

  The Status page is on `http://127.0.0.1:8080/`, and the editor's `PUT /api/profiles` works as on the Pico.
- **Ctrl-C** reads the AC. If it is on, the runner prints it and offers an off write (default: leave it, the
  "Pico switched off mid-cycle" case).
- **Before each real session:** the cases and the writes they make are agreed; afterwards
  `python3 tools/daikin_status.py <ac-ip>` shows the AC as it was found. The codec writes power, mode, target and fan
  only, so mold-proof and the streamer are never touched.

## Cases

Times are controller minutes. Events are the `kind`s in `events.log` after `boot`, in order. The simulator
results are from `make supervised SIM=1` with the default 15 s minute, 2026-10-19, and the first, shorter session
config (cool 8 → fan 4, hold 10, cap 30); every session there ended with the fake AC off and `writes approved` equal
to the writes the fake AC received. Real-AC session 1 (H1, H8, H12, 2026-10-20) also ran on that config; its writes
came 1.5 to 3 real minutes apart, which led to the 4-minute gap above.

The host half closed after session 1. That session showed what only the real AC can: it accepts the codec's writes
and reports them back (every write read back, `write_errors` 0), declined writes stay apart from human changes, and
the AC ends as found. The other cases exercise the controller and the client's error paths, which run unchanged
against the simulator; the remote and the Daikin app showing up in a read is in [DAIKIN_API.md](DAIKIN_API.md) (the
read tests). So H2–H7 and H9–H11 stand on their simulator result. The cases that need the board (P1–P4) and a short
pass of the H cases on the Pico are left for one acceptance session once the Pico writes, with the owner present.

| # | Case | Steps | Expected events; final AC | Host (sim) | Host (real AC) | Pico |
|---|---|---|---|---|---|---|
| H1 | Full cycle, trigger and `run` | start with the AC off; approve each write; after the cooldown, `run` rule 0 | `cycle_start` → `phase` (fan) → `cycle_end` → cooldown → idle, twice; every write read back (`write_errors` 0); off | pass: 2 cycles, 6 writes, 0 errors | pass (session 1): trigger cycle, then `run`; 6 writes approved, 6 accepted and read back; off (fan, fan 3), as found | | |
| H2a | Remote press during the cool phase | change target or fan on the remote | `manual`, no write on top; after the 16 min hold `takeover` → fan → off → `cycle_end`; off | pass: `manual` 1 poll after the press; takeover fan, off | sim only | |
| H2b | Remote press during the fan phase | change the fan speed | `manual` → hold → `takeover` → off, no fan phase (not wet); off | pass: takeover wrote off directly | sim only | |
| H3 | Remote off during a phase | switch off on the remote | `manual`; 5 min off → idle, no write; off | pass: idle with no write (run with H4b) | sim only | |
| H4a | Remote on during cooldown | after H1's `cycle_end`, switch on (cool) | `manual: AC on during cooldown` → hold → `takeover` → fan → off; off | pass: 2 takeover writes (fan, off) | sim only | |
| H4b | Remote on while idle | after H3's idle, switch on (dry) | `manual: AC on, not started by us` → hold → `takeover` → fan → off; off | pass: in the H3 run | sim only | |
| H5 | Daikin app change mid-phase | change the target in the app | `manual`, then as H2a; off | pass: an app-style `multireq` write to the fake AC | sim only | |
| H6 | AC on at runner start | start with the AC on (cool) | `manual: AC on, not started by us` → hold → `takeover` → fan → off; off | pass: 2 writes (fan, off) | sim only | |
| H7a | Restart mid-cycle, AC untouched | Ctrl-C during cool, leave it on; start the same session; Ctrl-C after `cycle_end` | boot 2: `phase` (the rule's fan, in full) → `cycle_end`; off | pass; then the trigger ran again 3 min later (F1) | sim only | |
| H7b | Restart mid-cycle, AC changed while down | as H7a, change the AC before the restart | boot 2: `manual: AC changed while the controller was down` → hold → `takeover` → fan → off; off | pass: 2 writes (fan, off) | sim only | |
| H8 | Two declined writes | answer `n` twice | `write_failed: … not applied twice` → `MANUAL` (never `manual`) → idle once off for 5 min; never written; off | pass: idle after the 5 min release; the AC never written | pass (session 1): `write_failed`, MANUAL, no `manual`; idle 81 s later; the AC never written | | |
| H9 | AC unreachable at the end of a phase | fault on before the cool phase ends, off after 3+ polls | `unreachable` after 3 reads; the phase ends on the first good read, its write goes out then → `cycle_end`; off | pass: fan written on the first good read | sim only | |
| H10 | Mac Wi-Fi off mid-cycle | Wi-Fi off for 3 min during cool (sim: `{"unreachable": 3}`) | real timeouts → `unreachable` → recovers, as H9; off | pass: same as H9, through HTTP timeouts | sim only | |
| H11 | Hard cap | after a full trigger cycle in the same session (its 240 min interval keeps it from starting again; a `stop_now` would write off seconds after the on), `run` rule 1; fault on for 2 polls across the end of its cool phase (the phases fit the cap, so only a held-up phase end reaches it; at most 2 real minutes late, so the fan still gets 4) | `cycle_start` (cap) → `phase` (fan, late) → `hard_cap` at 48 min → off written directly, the rest of the fan skipped → `cycle_end`; off | pass: the cap cut the fan after 4 of its 8 min | sim only | |
| H12 | Profiles saved mid-cycle | during H1's cool phase, PUT the profiles with rule 0's target 25 and fan 5 | `config_saved`; the running cycle keeps cool 26 / fan 3; the next `run` writes cool 25 / fan 5; off | pass: second cycle wrote cool 25, fan 5 | pass (session 1): saved 33 s into the trigger cycle, which kept 26 / auto and fan 3; the next `run` wrote cool 25, fan 5 (only the cool phase changed, so the AC ended off (fan, fan 3), as found) | | |

## Findings

- **F1: `min_interval_min` doesn't survive a restart.** The rule selector keeps each rule's last start in RAM as
  monotonic ms, so after a reboot every rule may start again at once (H7a: the trigger ran again 3 min after the
  resumed cycle ended, inside its 60 min interval). Cooldown, hold and the hard cap are unaffected. Open, decided in
  M5: persist the stamp (needs wall time, which the Pico only has after NTP) or document it as accepted, like the RAM history.

The Pico half adds, once the Pico writes:

| # | Case | Expected |
|---|---|---|
| P1 | Pico power off mid-cycle and back on | AC untouched → finished (fan, off); AC changed → hold |
| P2 | Wi-Fi off on the device mid-cycle | `unreachable`, timers keep running; reconnects and the cycle ends off |
| P3 | Router restart while idle | reconnect without a reset in under 15 min |
| P4 | New AC IP | one reset, then `unreachable`, no reset loop |
