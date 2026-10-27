# Temperature Target Mode (`temp_control`)

A second automatic operating mode next to `timed_close` (formerly `vent_closer`). Instead of closing the air vent along a
fixed line over `VENT_CLOSE_TIME`, it watches the max temperature of the glass-door thermal frame:

1. **Flaming stage:** close the vent as fast as the fire allows while holding the temperature at
   a target (default 300°C). Back off if the closing starves the fire.
2. **Coal stage:** once the wood has burned down, close at the linear closer rate to minimize
   chimney heat loss and make the coals last.
3. **Can't reach target:** raise a **Fire Stalled** flag for an HA notification and close at the
   linear closer rate.

The aim is robustness across load sizes and wood types without manual intervention, rather than
a large efficiency gain.

Code: `firmware/temperature_target.py` (controller), wired in `code.py`, `homeassistant.py`,
`thermal_camera.py`, `vent_closer.py` (all in `firmware/`). Offline tools: `simulation/`.

## Combustion background

- Wood burns in two stages. First the wood releases volatile gases that burn as flames; these
  carry a large share of the energy and burn cleanly only when hot (roughly 600°C / 1100°F or
  more) and with enough oxygen. Starving this stage gives smoke, CO and creosote, and can stall
  the fire.
- Then the remaining charcoal glows. Its burn rate is set almost entirely by the air supply.
- Air beyond what combustion needs is heated and sent up the chimney (*excess air*, the main
  heat-transfer loss). So in the **flaming stage** the goal is the least air that keeps the fire
  hot (the target temperature). In the **coal stage** the goal is simply the least air: that both
  cuts chimney loss and slows the coals' burn.
- An EPA non-cat stove's secondary air isn't controlled by the vent, and "closed" still leaks some
  primary air, so the vent trims the fire but never shuts it off.

## What the recorded data says (≈270 burn cycles, Dec 2025 – Oct 2026)

| Finding | Value | Design consequence |
|---|---|---|
| Max-pixel noise per 10 s sample | median 0.15°C, 10% of burns > 1.6°C, 1% > 3.3°C | Smoothing plus a noise band; must tolerate ~3°C |
| Chessboard (subpage) artifact frames | ≈1 per burn | Reject on-device before control |
| Response to a manual vent opening | dead time 2–3 min, 63% response in 6–10 min | Predict ahead; act on trends, not samples |
| After full close, T keeps rising | often +30–60°C | Start closing *before* the target |
| Closing a young fire too early | 114 manual interventions in 68 burns | Ignition phase; back off when closing starves the fire |
| Many loads peak below 300°C | about half of burns | Need an explicit "can't reach target" outcome |

### When have the reloaded logs caught?

Across 184 reloads, you closed the vent ≥15% within 5 minutes in 58% of them (median 101 s, to
about 25% closed). A fixed hold time can't follow that. Neither the starting glass temperature nor
the first minutes' heating rate predicted a stall. But the rise above the post-reload low did:

| First closing step came… | Reloads | Needed a manual rescue |
|---|---|---|
| before a 40°C rise above the post-reload low | 144 | 13% |
| after it | 31 | 3% |

The post-reload low comes about 1.4 min after reloading and is only ~2°C below the reload
temperature. The 40°C rise follows at a median of 7 min (25–75%: 5–10 min), never in 5% of
reloads. The glass lags the fire, so the camera can't confirm ignition sooner than about 4–5 min
even with ample coals. An earlier manual close is accepted, and a sag after it gets a back-off.

### Stall vs coal stage

Both look like "temperature falling below target", but need opposite responses (more air vs
less). Compared across your manual rescues and ≈2,600 samples of normal coal-stage cooling:

| | Stall (you re-opened) | Coal-stage cooling |
|---|---|---|
| Cooling rate | −1.5°C/min | −1.2°C/min |
| Hot region position/size on the glass | same | same |
| **Time since the vent last closed a step** | **2 min** median (75% < 18 min) | **87 min** median (75% > 55 min) |
| **Drop from the burn's peak** | **26°C** median | **110°C** median |

The camera can't tell flames from coals through the glass. What separates them is cause and
effect: a stall follows a closing step within minutes, while coal-stage cooling happens long after
the vent stopped moving. The controller uses exactly that.

## Control law

Runs once per accepted camera frame (≈10 s). The variables:

| Symbol | Meaning | In the code |
|---|---|---|
| `u` | Vent position, the controller's output: 0 = fully open, 1 = fully closed (as `Vent`). Closing is `u` going up. | `position` |
| `du` | Change of `u` in one update; positive closes | `du`, `step` |
| `T` | Smoothed max glass temperature, °C | `filtered_temp` |
| `T_pred` | `T` predicted `TT_HORIZON` seconds ahead | `predicted_temp` |
| `e` | Error: `T_pred − target`, °C. Positive means the fire is heading over the target, so close; negative means below target. | `error` |
| `Δe` | Change of `e` since the previous update | `error - last_error` |
| `dt` | Seconds since the previous update, capped at 30 | `dt` |

`e` uses the prediction, not the measured temperature: the glass answers a vent move only after
2–3 minutes, so acting on `T` alone would close too late and overshoot.

**Signal conditioning.**
- Reject chessboard frames (`subpage_mismatch > THERMAL_SUBPAGE_THRESHOLD`) and single-sample
  jumps > `TT_SPIKE_THRESHOLD` (door opened). A jump that persists for 3 samples is accepted.
- Exponential smoothing (`TT_FILTER_TIME`).
- A fast slope over `TT_SLOPE_WINDOW` for prediction, and a slower trend over `TT_TREND_WINDOW`
  for rising/falling decisions.
- Prediction: `T_pred = T + TT_HORIZON · slope`.

**Phases** (shown in HA as Controller Phase):

| Phase | Entered when | Vent |
|---|---|---|
| `ignition` | Reload (vent opened fully) | Hold until the logs have caught: rising and `TT_IGNITION_RISE` above the post-reload low (capped at `TT_IGNITION_LOW_CAP`); after a cold start (low below `TT_COLD_LOW`) rising and at `TT_COLD_IGNITION_TEMP`; or level within `TT_MARGIN` of target |
| `regulate` | Ignition complete | Close to `TT_EARLY_POSITION` if the logs caught; then the `TT_REGULATE` strategy: `predict` (close-only PI) or `trim` (linear closer plus a probed trim). Both only close. |
| `recovering` | Below `target − TT_MARGIN` and falling for `TT_CONFIRM_TIME`, within `TT_STALL_WINDOW` of a closing step (the controller's or yours) | Back off: re-open to where the vent was `TT_STALL_WINDOW` ago, at most `TT_BACKOFF_LIMIT`; wait up to `TT_RECOVERY_TIME` |
| `stalled` (**Fire Stalled** ON) | Logs haven't caught by `TT_STALL_MIN_TIME` and the temperature is level or falling; or the fire peaked and is falling without ever getting within `TT_COALS_MARGIN` of target; or starved again after `TT_MAX_RESCUES` back-offs | Close at the linear rate |
| `coals` | Peaked within `TT_COALS_MARGIN` of target, then below `target − TT_MARGIN` and falling with no closing step to blame (see the strategies below); or a back-off didn't help | Close at the linear rate; never re-open |

What happens next:

- From `recovering`, if the temperature rises again, the controller goes back to `regulate`.
  If more air doesn't help, the fire is fuel-limited, not starved: it goes to `coals` if the
  target was reached, otherwise `stalled`.
- From `stalled`, if the fire recovers, it goes back to `regulate`. Recovery means rising for
  `TT_CONFIRM_TIME`, and either `TT_STALL_RECOVERY_RISE` above its low while stalled or within
  `TT_MARGIN` of target (e.g. after adjusting the logs or adding kindling). Below target,
  `regulate` holds the vent, so the recovery isn't closed off. Closing steps made while stalled are
  kept, so a relapse soon after gets a back-off, and a relapse with no response returns to
  `stalled`.
- A reload is always signalled by opening the vent fully. A partial opening only adjusts airflow
  for the existing logs and doesn't restart the burn.

**Linear rate.** The remaining open fraction closes over that fraction of the HA Closing Duration:
the same slope as the linear closer, which now sets this rate in both modes.

### Regulate strategy `predict`: close-only PI controller

The default (`TT_REGULATE = "predict"`), and the strategy the simulation results below are for.
Each update:

1. **PI step, velocity form:** `du = Kp·Δe + Ki·e·dt/60`
   (`TT_KP` = 0.01 per °C, `TT_KI` = 0.006 per °C per minute, `dt` in seconds).
   - The **integral term** `Ki·e` closes at a speed proportional to how far the prediction is
     over the target. At `e = +20°C` that is 0.12 per minute (12% of the vent's travel).
   - The **proportional term** `Kp·Δe` reacts to the prediction changing. A prediction that
     jumps up 5°C between samples closes 0.05 at once; one that falls opens the PI output by as
     much. Since `T_pred = T + horizon·slope`, a change in `e` mostly means a change in the
     slope, so this term works like derivative action on the temperature.
2. **Rate limit:** `du` is clipped to ±`TT_MAX_CLOSE_RATE`·`dt`/60: at most 0.2 per minute, or
   0.033 per 10 s sample. The integral term saturates from `e` ≈ +33°C.
3. **Below target never closes:** if `e < 0`, `du` is limited to ≤ 0. Without this, the
   proportional term closes the vent while a young fire's prediction is still climbing toward
   the target (seen on the first live burn, 17 Oct 2026).
4. **Close only, with a noise band:** the PI keeps its own output `u_PI` (`pi_position`),
   limited to at most `TT_NOISE_BAND` (0.1) below the vent:
   `u_PI = min(1, max(u − TT_NOISE_BAND, u_PI + du))`. The vent follows only when `u_PI` goes
   above it (`u = u_PI`); it never re-opens in this phase. Re-opening is the separate,
   deliberate back-off of the `recovering` phase.

Why the noise band: if `u_PI` were clipped at `u`, every negative `du` from sensor noise would
be thrown away and every positive one kept, so symmetric noise would ratchet the vent closed.
With the band, opening `du`s are stored as up to 0.1 of slack below the vent, which later
closing `du`s must use up before the vent moves.

Why the velocity form: the controller computes moves, not an absolute position, so it carries no
integrator state that could wind up while the vent is clipped (closed, rate-limited or held below
target), and a manual move is bumpless: `u` and `u_PI` are set to the new position, and `Δe` is
skipped for one sample to avoid a kick.

A typical burn: the prediction crosses 300°C while the glass is still climbing (about 200–250°C),
and the vent closes in a staircase at up to 20%/min. It pauses whenever the climb slows enough
to bring the prediction under the target, and resumes when it picks up. A stall test runs every
update: below `target − TT_MARGIN` and falling for `TT_CONFIRM_TIME`, with a closing step in the
last `TT_STALL_WINDOW` (any closing step, the PI's or yours), backs off. With no closing step to
blame it is `coals` (peaked near the target) or `stalled`.

### Regulate strategy `trim`: linear closer with a probed trim

Experimental (`TT_REGULATE = "trim"`). The stove runs this mode since 18 Oct 2026. Instead of
deciding from the prediction when to close, it follows the timed closer and only adjusts it:

`u = baseline + trim + probe`, clipped to 0..1.

- **Baseline:** starts where the vent is when regulating starts (after the early close, a
  manual move, a back-off or a recovery), and closes at the linear closer rate: `dt / time_range`
  per update, where `time_range` is the HA Closing Duration. At 35 min that is 0.005 per 10 s
  sample, 0.086 per 3 minutes.
- **Guard (integral only):** while the prediction is over the target (`e > 0`), the baseline
  also closes by `min(TT_MAX_CLOSE_RATE, Ki·e)·dt/60`. The prediction looks only 120 s ahead here
  (`TT_HORIZON` defaults to 120 in trim mode, not 450): on 18 Oct 2026 the 450 s prediction of a
  fast climb closed the vent at full speed just before the peak. A guard step that moves the vent
  is a *deliberate close* for the back-off rule below.
- **Probe:** while the fire is below `target − TT_MARGIN` and not falling, the vent alternates
  between `+d` (more closed) and `−d` around `baseline + trim`, switching every
  `TT_PROBE_PERIOD` (180 s). The amplitude `d` is `TT_DITHER` (0.05), raised so that each switch
  from `+d` to `−d` still re-opens the vent past the actuator's 0.07 deadband after the baseline's
  closing over one period: `d = max(0.05, (0.07 + period/time_range)/2 + 0.01)`, which is 0.088 at
  a 35 min Closing Duration. Each period yields the glass rise rate (°C/min, least squares from
  `TT_PROBE_DELAY` = 60 s after the switch, skipping the response delay) and the mean commanded
  position over the same samples.
- **Trim decision:** from the last three periods `(p0, r0), (p1, r1), (p2, r2)` (position,
  rise rate), second differences cancel any steady drift: the baseline's creep in the positions,
  and the fire's own acceleration or slowing in the rates.
  - `curvature = 2·p1 − p0 − p2`. With alternating signs this is about `4d`; the decision is
    skipped if it is under `d`.
  - `gain = (2·r1 − r0 − r2) / curvature`: rise rate change per unit of vent closed.
  - `effect = gain · d`. If `|effect| ≥ TT_TRIM_THRESHOLD` (0.5°C/min), the trim moves one `d`
    toward the faster climb (closing helped: `+d`; opening helped: `−d`), within
    ±`TT_TRIM_BAND` (0.15).

  The first decision comes after three full periods (9 minutes), then one per period on a sliding
  window. This is extremum seeking: it looks for the vent position where the fire climbs fastest,
  which isn't always the most open one. Excess air cools the fire: in the recorded burns, closing
  from mostly open below 200°C sped up the climb in 80% of steps (`simulation/vent_steps.py`).
  At 200°C and up, closing made no difference on average.
- **Back-off:** confirmed below `target − TT_MARGIN` and falling backs off only if there was a
  deliberate close within `TT_STALL_WINDOW`, or the fire never came within `TT_COALS_MARGIN` of
  the target and the vent closed beyond the creep in that window. A deliberate close is the early
  close, a guard step that moved the vent, or a manual close of 0.05 or more before the fire came
  near the target. Otherwise the decline is `coals` (peaked near the target) or `stalled`. The
  schedule's routine closing doesn't count: on 18 Oct 2026 both burns backed off as their fuel ran
  out. A guard step holding an already closed vent doesn't count either (24 Oct 2026), and nor
  does a manual close after the peak, which says the fire is burning down.
  `simulation/trim_cases.py` checks these decisions.

A back-off, a manual move or a recovery rebases: `baseline` is set to the vent position, and the
trim and probe history are cleared.

Status: in the burns since 18 Oct 2026 the trim has not changed once (the first two decisions,
before the dither fix, were noise). The probe acts on minute-scale responses to vent steps, which
none of the fitted stove models reproduce (see `simulation/stepcheck.py`), so simulation can't
tune or validate it; only real burns can.

**Manual moves** go through the existing `Override` / `MoveVentState` paths:
- Opening to < 10% open counts as a reload and restarts ignition.
- Any other move continues from the new position. Closing by hand counts as a closing step when
  attributing a later drop.

**Sensor loss.**
- No valid sample for `TT_STALE_TIMEOUT`: the vent is held and a warning logged.
- After `TT_STALE_FALLBACK_TIME`, it closes at the linear rate, like `timed_close`, until samples
  return. This avoids holding e.g. a fully open vent on a fresh load while the camera is out.
- After any gap, a single control update acts on at most 30 s, so a blocked main loop doesn't
  release a minute's worth of closing at once.
- Camera recovery power-cycles the STEMMA QT port (`NEOPIXEL_I2C_POWER`) from the second attempt
  on, which resets a hung MLX90640 the way a soft reboot does. It retries at least every 60 s.

**Logging.** Every phase change and controller action (reload, early close, back-off, manual
move) writes one INFO line from the `temperature_target` logger to the MQTT log and file log:

```
temp_control regulate -> recovering: starved by closing, back-off 0.850 -> 0.600 | t=1930
  phase=recovering T=268.4 pred=241.0 trend=-1.9/min peak=301.2 low=142.0 target=300 reached=1
  vent=0.600 since_close=140 rescues=1
```

(Shown wrapped here; it is a single line in the log.)

`t` is seconds since the reload, the same clock as the StoveLink `combustion_time` stored in each
HDF5 frame. Log lines can be matched to frames without relying on wall-clock time.

The controller reuses `VentCloser` and its Monitoring/Closing/Override/Closed sub-states
unchanged; `TemperatureTargetFunction` replaces `LinearVentFunction`.

## Using it

- HA "State Machine" select options are now `idle`, `timed_close` and `temp_control`.
  MQTT `<topic>/command` accepts the same names; `vent_closer` and `close` still work as aliases
  for `timed_close`.
- `AUTO_MODE = "temp_control"` in `settings.toml` makes Idle engage this mode on the
  closed→open reload gesture (default stays `timed_close`).
- **Closing Duration** (existing number) now sets the linear rate for both modes.
- New entities:
  - **Target Temperature** (number, 150–400°C)
  - **Fire Stalled** (binary sensor, problem class)
  - **Controller Phase** and **Predicted Max Temperature** (diagnostic sensors)
- Notification automations:
  - **Fire Stalled → on**: the load can't reach target.
  - **Controller Phase → `coals`**: optional "reload now?" reminder.

### Settings (`settings.toml`, all optional)

| Key | Default | Meaning |
|---|---|---|
| `TT_TARGET` | 300 | Target max glass temperature, °C |
| `TT_MARGIN` | 25 | °C below target that counts as "not at target" |
| `TT_REGULATE` | predict | Regulate strategy: `predict` (close-only PI) or `trim` (linear closer plus probed trim) |
| `TT_KP` | 0.01 | Vent fraction per °C change in predicted error (`predict`) |
| `TT_KI` | 0.006 | Vent fraction per °C of predicted error per minute (`predict`; `trim`'s guard) |
| `TT_HORIZON` | 450 (120 in `trim`) | Prediction horizon, s |
| `TT_FILTER_TIME` | 60 | Smoothing time constant, s |
| `TT_SLOPE_WINDOW` | 120 | Prediction slope window, s |
| `TT_TREND_WINDOW` | 300 | Rising/falling decision window, s |
| `TT_PLATEAU_SLOPE` | 0.5 | °C/min: slower counts as flat |
| `TT_CONFIRM_TIME` | 120 | A decline/plateau must persist this long, s |
| `TT_MAX_CLOSE_RATE` | 0.2 | Max closing speed while regulating, fraction/min |
| `TT_NOISE_BAND` | 0.1 | PI output slack below the vent position |
| `TT_IGNITION_RISE` | 40 | °C above the post-reload low that marks the logs as caught |
| `TT_IGNITION_LOW_CAP` | 150 | Cap on that low, so a hot-stove start or mid-burn restart isn't stuck (caught by 190°C) |
| `TT_EARLY_POSITION` | 0.25 | Partial close when the logs have caught; 0 disables it |
| `TT_COLD_LOW` | 60 | A post-reload low below this (°C) is a cold start |
| `TT_COLD_IGNITION_TEMP` | 150 | After a cold start, the logs count as caught at this °C (rising) |
| `TT_COALS_MARGIN` | 50 | A fire that peaked within this of target (°C) burns down to `coals`, not `stalled` |
| `TT_TRIM_BAND` | 0.15 | `trim`: max trim either way |
| `TT_DITHER` | 0.05 | `trim`: minimum probe amplitude (raised to clear the actuator deadband) |
| `TT_PROBE_PERIOD` | 180 | `trim`: seconds per probe half-cycle |
| `TT_PROBE_DELAY` | 60 | `trim`: response delay skipped before measuring a period's rise rate, s |
| `TT_TRIM_THRESHOLD` | 0.5 | `trim`: rise rate effect (°C/min) of one dither that moves the trim |
| `TT_STALL_MIN_TIME` | 900 | Earliest "logs haven't caught" stall, s |
| `TT_STALL_RECOVERY_RISE` | 20 | °C above the low while stalled, rising, to regulate again |
| `TT_STALL_WINDOW` | 1200 | Decline within this long of a closing step is a stall, s |
| `TT_BACKOFF_LIMIT` | 0.25 | Max re-opening per back-off |
| `TT_RECOVERY_TIME` | 600 | Time for the fire to respond to a back-off, s |
| `TT_MAX_RESCUES` | 2 | Back-offs per burn before declaring a stall |
| `TT_SPIKE_THRESHOLD` | 60 | Outlier rejection, °C |
| `TT_STALE_TIMEOUT` | 120 | Hold the vent if no samples for this long, s |
| `TT_STALE_FALLBACK_TIME` | 300 | Then close at the linear rate until samples return, s |
| `THERMAL_SUBPAGE_THRESHOLD` | 10 | Chessboard frame rejection, °C |
| `AUTO_MODE` | timed_close | Mode engaged by the reload gesture (`vent_closer` still accepted) |

## Validation

### Replay of recorded burns (`simulation/replay_stalls.py fair`)

The recorded temperatures go through the real classifier. Recorded closing steps count only where
the controller itself would have closed (predicted temperature at or above target).

**Burns you left alone after the peak (116):**
- 90 classified as coal stage.
- 17 flagged Fire Stalled. 8 were not real fires (peaks of 30–64°C) and 6 were weak loads that
  peaked at 200–272°C. 3 were flagged early (peaks 280–320°C); the linear closer was choking
  those, and the flag clears if the fire climbs back near target.
- 8 got a back-off; those that had reached target resolve to coal stage.
- 1 never declined clearly.

So Fire Stalled fires for weak loads and essentially never on a normal burn.

**Your manual rescues of a sagging fire (18 first rescues):**
- Where the controller's logic applies, it backed off a median 15 minutes before you did.
- The 8 it missed were young fires (8–26 min, < 280°C) that the linear closer choked.
  temp_control would still be holding them open in ignition.

### Closed-loop simulation (`simulation/final.py`)

Two grey-box stove models were fitted to 90 burns:
- **v2:** fuel-dominated and slow (median error 8°C).
- **v3:** air- and fuel-limited burn rate plus a fast flame term (median error 12°C).

Neither is right on its own; they bracket the behavior. Results are means over all burns and
parameter variants, with a 60 s extra actuator delay:

| | Linear v2 | Temp target v2 | Linear v3 | Temp target v3 |
|---|---|---|---|---|
| Time within 15°C of / above target | 39 min | **51 min** | 38 min | **46 min** |
| Time > target + 40°C | 14 min | **13 min** | 16 min | **10 min** |
| Time < 200°C with > 30% fuel left (starved) | 32 min | **20 min** | 23 min | **22 min** |
| Mean air while burning (lower is better) | **0.58** | 0.64 | **0.33** | 0.37 |
| Burns flagged Fire Stalled | n/a | 8% | n/a | 25% |

Ending ignition on the 40°C rise gives the same results as the earlier fixed 10-minute hold.
Adding `TT_EARLY_POSITION=0.25` saves 3–5% air but costs 1–4.5 min at target and gives more stall
flags in the models. The models don't know the coal bed, though. The recorded early closes say
otherwise (closing from wide open sped up the climb in 79% of them), so 0.25 is the default
since 18 Oct 2026. The results in this section are from before that change, with
`TT_EARLY_POSITION=0`.

The controller spends more time at target, starves the fire less and overshoots less. It uses a
few percent more air, mostly because the coal stage now closes at the linear rate instead of
already being closed. **Noise robustness:** at 0.5, 3 and 8°C sensor noise, time at target is
45, 45 and 43 min, with no extra false stall flags.

![v3 trajectories](temperature_target_sim_v3.png)

Gray = linear closer, red = temperature target; solid = max temp, dotted = fraction open.

## How the controller was chosen and tuned

### Requirements

From the design questions of 17 Oct 2026:
- **Goal:** close the vent as fast as the fire allows while holding the glass near 300°C. The aim
  is robustness across load sizes and wood types, so burns need fewer manual interventions,
  more than an efficiency gain.
- **Soft target:** overshoot is acceptable, with no over-fire limit.
- **Close only:** don't open past where the vent was. Below target, close at a reasonable rate
  instead of all at once, and raise **Fire Stalled** when the target can't be reached.
- **The stove:** an EPA non-cat stove, so "closed" still leaks air. The camera sees the glass
  door.

### What the plant looks like

From the recorded burns (tables above):
- Dead time of 2–3 min and a 6–10 min lag.
- 30–60°C of further rise after a full close.
- Little sensor noise.
- The process changes through every burn: fuel burns out, and the same vent move speeds up a
  young fire but starves a weak one. No fixed linear model describes it.

### Choices

- **A phase state machine around a simple controller.** The hard decisions are discrete: when
  the logs have caught, and whether a drop is starvation (re-open) or the coal stage (close).
  They need opposite actions, so they are rules fitted to the recorded interventions, not
  something a continuous control law could learn. The continuous part only has to answer "how
  fast to close".
- **PI rather than model-based control.** A Smith predictor or model-predictive control needs a
  plant model, and the best fitted models miss by 8–12°C (median) and get the response to a vent
  step wrong. PI needs no model. The integral term finds whatever position holds the target for
  this load, with no steady offset. A derivative term on raw samples would amplify noise.
- **Prediction as dead-time compensation.** Acting on `T + horizon·slope` starts closing while
  the glass is still climbing, so the 30–60°C of post-closing rise lands near the target instead
  of above it. The slope is a least-squares fit over 120 s of smoothed samples, which tames the
  noise a derivative would amplify.
- **Close-only, velocity form.** From the requirements. Re-opening happens only through the
  rule-based back-off, with its own limits (`TT_BACKOFF_LIMIT`, `TT_MAX_RESCUES`). The velocity
  form makes the close-only clipping and manual moves safe (no windup, no bump), and the noise
  band stops the clipping from ratcheting the vent closed on noise.
- **The rate limit** (`TT_MAX_CLOSE_RATE`, 0.2 per minute, a full stroke in 5 min) bounds how
  hard one bad prediction can close.

`trim` came later, from the first live burns and the vent-step analysis of 18 Oct 2026:
- `predict`'s long horizon closed at full speed just before a fast climb peaked.
- Both burns that day backed off as their fuel ran out.
- Closing from mostly open sped up young fires, which a close-only controller that pauses below
  target can't exploit.

So `trim` keeps the timed closer's predictable schedule, which you already trusted, and only
measures and corrects around it. It is experimental and not the default.

### Tuning

1. **Stove models.** `simulation/fit.py` fits grey-box stove models to 90 recorded burns
   (global parameters plus each burn's fuel load and initial fire):
   - v2 is fuel-dominated, with a median error of 8°C;
   - v3 has an air- and fuel-limited burn rate plus a fast flame term, 12°C;
   - v4 adds excess air, 15.5°C RMS.

   v2 and v3 disagree in opposite directions, so tuning uses both. Parameter variants (time
   constants ×0.5 and ×2, and so on) and a 60 s extra actuator delay test robustness.
2. **Closed loop with the real code.** `simulation/closedloop.py` runs
   `firmware/temperature_target.py` against the models. The metrics are:
   - mean air while burning;
   - minutes within 15°C of the target or above it;
   - minutes more than 40°C over it;
   - minutes starved (below 200°C with over 30% of the fuel left);
   - the time to close, and how often the fire is flagged stalled or backed off.
3. **Grid search** (`simulation/tune.py`) over horizon {300, 450, 600} s, max close rate
   {0.1, 0.2}/min, `Kp` {0.006, 0.015}, `Ki` {0.003, 0.006} and margin {20, 30}°C, 48
   combinations on both models.
   - The score is the extra air over the linear closer, plus 0.01 per minute lost at the target
     and 0.01 per extra minute starved. Lower is better.
   - The best 8 scored 0.040–0.046, so the optimum is flat: the result depends on the horizon
     and `Ki` more than on `Kp` or the margin.
   - All of the best 8 use the longest horizon (600 s) and the larger `Ki` (0.006), which is the
     top of the range searched.
4. **Noise sweep** at 0.5 and 3°C sensor noise, over the filter time, noise band and horizon.
   At 3°C:
   - The 600 s horizon lost time at the target: on v3, 37.8 min against 42.6 min with 450 s, and
     a back-off in 20% of burns against 7%.
   - A noise band of 0.05 lost more than 0.1.
   - Filter times of 30–90 s mattered little.

   So the defaults are horizon 450 s, filter 60 s, band 0.1, max close rate 0.2, `Ki` 0.006,
   `Kp` 0.01 (between two values that scored the same) and margin 25.
5. **Thresholds from the data, not the models.** The ignition rise (40°C), the stall window
   (1200 s), the coal-stage test and the early close position (0.25, from
   `simulation/vent_steps.py`) come from the recorded burns and your interventions, checked with
   `simulation/replay_stalls.py`. The models can't judge them.
6. **Live burns.** Each of these later changes came from a live burn: the below-target rule
   (17 Oct), the cold-start ignition, the coals margin, the early close, trim's short horizon
   and its dither fix (18 Oct), and trim's back-off rules (18 and 24 Oct).
   `simulation/restart.py` and `simulation/trim_cases.py` guard them.

What simulation can't settle: the models don't reproduce the minute-scale response to a vent
step (`simulation/stepcheck.py`: the correlation with the measured responses is −0.07 for v3 and
−0.21 for v4). They can rank strategies on the slow behavior (time at target, air, overshoot),
but not tune trim's probe, or anything else that acts on short-term responses. On the v4 model
(`simulation/compare.py`), trim and predict trade wins depending on the variant.

## First-burn test plan

1. Keep `AUTO_MODE = timed_close`; select `temp_control` manually from HA after a reload.
2. Watch Controller Phase, Fire Stalled, Predicted Max Temperature, Thermal Max and Air Vent.
   Expected sequence:
   - `ignition` until the glass has risen 40°C above its post-reload low (typically 5–10 min);
   - a staircase of closing steps once the prediction crosses 300°C, with pauses on dips;
   - `coals` with a steady linear close.
3. Create the HA notification automation on Fire Stalled.
4. Match the `temp_control` log lines to the HDF5 frames by `t` = `combustion_time`.
5. Refit and re-tune from new StoveLink cycles with `simulation/fit.py` and `tune.py`, and
   re-check the classifier with `replay_stalls.py`.

## Caveats / open items

- **Sensor limit:** the MLX90640's rated object range ends at 300°C. Readings reach ~400°C, but
  above 300 they are outside spec. Don't raise `TT_TARGET` without that in mind.
- **Glass soot:** at low air the glass can soot up and read as cooling. Check the thermal images
  of early burns.
- **Small loads** behave much like the linear closer (with a notification). Adopting the peak a
  load actually reached as its target is a possible future refinement.
- **Coal stage and CO:** at low air the coals produce CO. A sealed stove with working draft sends
  it up the chimney, but keep a CO detector in the room as usual.
- **Logging:** controller internals aren't in the StoveLink packet. Adding the phase to the
  reserved header field would make analysis of real burns easier.
