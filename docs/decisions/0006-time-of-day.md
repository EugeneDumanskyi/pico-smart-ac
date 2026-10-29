# 0006: Time of day from the AC, for schedule stages only

- Status: accepted
- Date: 2026-10-28
- Amends: [ADR 0004](0004-time.md)

## Context

A day or a night is not one climate. After the first live days the owner wants Day and Night to change with the
hour: more fan time in the cool morning and evening, a shorter cooldown in the hot afternoon, a longer fan phase and
cooldown after midnight. Switching profiles by hand that often doesn't work, least of all while asleep.
The outdoor temperature can't stand in for the hour: the outdoor unit reads its own exhaust (38–43 °C) while it runs.

ADR 0004 kept the wall clock out because the Pico has no battery clock and NTP needs DNS, which blocks the loop.
But the indoor unit's status read, which the Pico already does every poll, carries `last_ope.time`: the local time
of the AC's last operation (docs/DAIKIN_API.md). It needs no internet, no time zone setting and no extra request.

## Decision

- **Durations stay on ticks.** Phases, cooldowns, holds, pauses and intervals keep ADR 0004 unchanged. The wall
  clock only picks which stage of a schedule runs; nothing is ever timed by it.
- **Main source: the AC.** `core/clock.py` syncs when `last_ope.time` changes between two reads (an operation just
  happened: ours shows on the read-back, ~5 s late; a remote press up to one poll late). The value seen first after
  a boot is an old operation and only becomes the baseline.
- **Backup: the phone.** The web page sends its local time; it sets the clock only until the AC has synced it, and
  is otherwise only compared.
- **Distrust jumps.** A sync more than 2 h from the running clock makes the time unknown until a second sync agrees
  with it within 10 min. A time before 2024 or one that doesn't parse is ignored.
- **Unknown time is normal.** Each schedule marks one stage as the default; it runs while the time is unknown, so the
  controller never stops for want of a clock.
- Events and `state.json` stay without timestamps; a clock event gives the time it read, as a message.

## Consequences

- Between syncs the clock runs on the crystal: seconds of drift a day, fine for stages set to the minute.
- After a reboot with the AC off and the page closed, the default stage runs until the next operation or page visit.
- Rolled out in steps: first the clock alone, logged only (`clock` events), to check it against real use before
  anything depends on it; then the phone sync, then schedules.
- No DST handling is needed: both sources are already local time.
- The default `day` and `night` profiles are schedules over ten stage profiles (docs/idea.md, "Profiles"). A board
  with a saved `profiles.json` keeps it until Restore defaults.
