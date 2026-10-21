# 0003: AC ownership and "the remote wins"

- Status: accepted
- Date: 2026-10-08

## Context

The AC is off by default and, while the Pico runs, the Pico owns it. The remote is one-way IR and re-sends its full
state on every press; the Daikin app can change the AC too. Neither tells us. The owner switches the Pico off at times,
also mid-cycle. Automation must never fight a human, and must not let a forgotten AC run for hours.

## Decision

1. **Ownership.** Automation owns the AC from the moment it starts a cycle until off is written and read back.
   It stores the exact state it set (`expected`: power, mode, target, fan) in RAM and `state.json`.
2. **Read before write.** Every write is decided from a read taken in the same control tick. If that read differs from
   `expected`, the write is dropped and the state becomes `MANUAL`.
3. **Read back.** ~5 s after a write, read again. A mismatch is a failed attempt, not yet a human: the AC may still be
   applying the write. The write is sent again on the next tick; a second failed attempt means a rejected write:
   `MANUAL` with a `write_failed` event. Human changes are detected on poll reads only (rule 2): a poll read that
   shows a state we didn't set, other than the old state of a write not yet applied, is `MANUAL`.
4. **Manual hold, then takeover.** An AC state we didn't set (changed during a cycle, or on when we were idle or just
   booted) gets `manual_hold_min` hands off; every new change restarts the hold. If the AC goes off and stays off for
   `manual_release_off_min`, back to `IDLE`. If the hold ends with the AC still on, `TAKEOVER`: fan for
   `finish_fan_min` if it was cooling or drying, then off. No takeover in the `off` profile or while paused.
5. **Stopping is a state.** `STOPPING` repeats the off write each tick until a read confirms it. `COOLDOWN` starts only then.
   Any read that shows the AC off ends the stop, whoever switched it off; failed attempts give an event, not `MANUAL`.
6. **Hard cap is absolute.** At `hard_cap_min` total cycle time, off is written directly, skipping any fan phase.
7. **Reboot.** If `state.json` says we own a cycle and the AC still matches `expected`, finish it at once: last fan
   phase, then off. A cool/dry phase is never resumed. If the AC is off and that is what we expected (our first write
   never landed, or a stop was under way), the cycle just ends: off, then cooldown. If it doesn't match, rule 4 applies.
8. **No backstop when the Pico is off.** Switching the Pico off mid-cycle leaves the AC to the remote. The AC's own
   off-timer is not used.
9. **Pausing or switching profile during a cycle** asks the user: *finish* (fan, then off) or *leave running*
   (we give up ownership; the AC is treated as a human's run once automation is active again). A finish ends without
   the cycle's cooldown. If a human changes the AC during a pause's finish, we let go and go straight to `PAUSED`.

## Consequences

- A remote press that sends exactly our state is invisible. Accepted for v1; the IR receiver (v1.x) fixes it.
- Override detection latency is one poll (≤ 60 s), but a write never lands on top of a change already seen.
- A human who wants the AC on for longer than `manual_hold_min` must press the remote again, or pause automation.
- Writes the AC rejects show up as `MANUAL`, not as silent loops.
