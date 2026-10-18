import os
import time

try:
    import adafruit_logging as logging
except ImportError:
    import logging

from vent_closer import VentFunctionABC

logger = logging.getLogger(__name__)

# Controller phases
PHASE_IGNITION = "ignition"
PHASE_REGULATE = "regulate"
PHASE_RECOVERING = "recovering"
PHASE_STALLED = "stalled"
PHASE_COALS = "coals"

# Longest time step (s) one control update may act on
MAX_CONTROL_DT = 30


class TemperatureTargetFunction(VentFunctionABC):
    """Vent function that closes the air vent as fast as the fire allows while holding the
    glass-door max temperature at a target, then minimizes air through the coal stage.

    Plugs into VentCloser in place of LinearVentFunction: get_position() returns the desired
    vent position (0.0 open .. 1.0 closed), start()/adjust() handle reloads and manual
    overrides. Temperature samples are pushed in with update_temperature() each time a valid
    thermal camera frame is captured; the control law runs once per sample.

    Phases:
      ignition   - hold the vent where it is (normally fully open after a reload) until the new
                   logs have caught: the temperature has risen ignition_rise above its lowest
                   point since the reload, capped at ignition_low_cap (in the recorded burns a
                   median of 7 minutes, sooner with a hot coal bed), or is already near the
                   target and level. Then optionally
                   close part way to early_position. If the logs haven't caught by
                   stall_min_time and the temperature is level or falling: stalled.
      regulate   - close-only PI on the *predicted* temperature error. Predicting horizon
                   seconds ahead (T + horizon * dT/dt) compensates the 2-3 minute dead time
                   between a vent move and the glass temperature response, so closing starts
                   before the target is overshot. Below target the controller just pauses.
      recovering - the temperature fell well below target shortly after the controller closed
                   the vent: the fire is being starved. Back off (re-open to where the vent was
                   stall_window ago, at most backoff_limit) and wait up to recovery_time for the
                   temperature to rise again; then resume regulating. If more air doesn't help,
                   the fire is fuel-limited: coals if the target was reached, otherwise stalled.
      stalled    - the fire can't reach or hold the target (fire_stalled is asserted for a
                   notification). Close at the linear closer rate: the remaining fraction open
                   takes that fraction of time_range (the HA Closing Duration). If the fire
                   recovers (rising, and stall_recovery_rise above its low while stalled or near
                   the target), regulate again.
      coals      - the target was reached and the temperature has been falling with no closing
                   step for stall_window: the wood has burned down to coals. Less air now both
                   reduces heat lost up the chimney and makes the coals last longer, so close at
                   the linear closer rate and never re-open.

    Telling a stall from the coal stage relies on cause and effect: in the recorded burns, the
    temperature drops that were rescued by hand followed a closing step by a median of 2 minutes,
    while normal coal-stage cooling came a median of 87 minutes after the last closing step.
    Neither the cooling rate nor the hot region on the glass distinguished them.

    The velocity form (move by du each sample instead of computing an absolute position)
    makes manual overrides bumpless and needs no integrator anti-windup: the controller just
    continues from wherever the vent actually is.
    """

    def __init__(
        self,
        target=None,
        time_range=None,
        time_func=time.monotonic,
        kp=None,
        ki=None,
        horizon=None,
        filter_time=None,
        slope_window=None,
        trend_window=None,
        max_close_rate=None,
        noise_band=None,
        ignition_rise=None,
        ignition_low_cap=None,
        early_position=None,
        stall_min_time=None,
        margin=None,
        confirm_time=None,
        plateau_slope=None,
        stall_window=None,
        backoff_limit=None,
        recovery_time=None,
        max_rescues=None,
        stall_recovery_rise=None,
        spike_threshold=None,
        stale_timeout=None,
        stale_fallback_time=None,
        regulate=None,
        cold_low=None,
        cold_ignition_temp=None,
        coals_margin=None,
        trim_band=None,
        dither=None,
        probe_period=None,
        probe_delay=None,
        trim_threshold=None,
        reload_threshold=0.1,
    ):
        def setting(value, name, default):
            if value is not None:
                return float(value)
            return float(os.getenv(name, default))

        # time_range: seconds for a full open->closed stroke at the linear closer rate, used in
        # the stalled and coals phases. Shared with the HA Closing Duration entity.
        super().__init__(
            time_range=setting(time_range, "VENT_CLOSE_TIME", 3600), time_func=time_func
        )

        # Regulate phase strategy: "trim" follows the linear closer with a bounded trim found by
        # probing the vent; "predict" closes only once the long-range prediction passes the target
        # (predict until trim is validated: see simulation/stepcheck.py)
        self.regulate = regulate or os.getenv("TT_REGULATE", "predict")
        trim_mode = self.regulate == "trim"

        # target: degC, max pixel of the glass-door thermal frame
        self.target = setting(target, "TT_TARGET", 300)
        # Proportional gain: vent fraction per degC of predicted error
        self.kp = setting(kp, "TT_KP", 0.01)
        # Integral gain: vent fraction per degC of predicted error per minute
        self.ki = setting(ki, "TT_KI", 0.006)
        # Prediction horizon (s): roughly the dead time plus a fraction of the lag
        # In trim mode only a guard against overshoot: a short look ahead, so a fast climb doesn't
        # look like an overshoot minutes early (18 Oct 2026: 450 s closed the vent at full speed
        # just before the peak)
        self.horizon = setting(horizon, "TT_HORIZON", 120 if trim_mode else 450)
        # Exponential smoothing time constant (s) applied to raw temperature samples
        self.filter_time = setting(filter_time, "TT_FILTER_TIME", 60)
        # Window (s) for the least-squares slope used for prediction (fast)
        self.slope_window = setting(slope_window, "TT_SLOPE_WINDOW", 120)
        # Window (s) for the slope used to decide rising/falling for phase changes (robust)
        self.trend_window = setting(trend_window, "TT_TREND_WINDOW", 300)
        # Max closing speed while regulating, vent fraction per minute
        self.max_close_rate = setting(max_close_rate, "TT_MAX_CLOSE_RATE", 0.2)
        # The PI output may fall this far (vent fraction) below the vent position without
        # re-opening it, so sensor noise moving the output up and down doesn't ratchet the
        # vent closed
        self.noise_band = setting(noise_band, "TT_NOISE_BAND", 0.1)
        # Rise (degC) above the lowest temperature since the reload that marks the logs as caught.
        # In the recorded burns, closing before a 40C rise needed a rescue 13% of the time,
        # closing after it 3%.
        self.ignition_rise = setting(ignition_rise, "TT_IGNITION_RISE", 40)
        # Cap (degC) on that lowest temperature. Starting on an already hot stove (a restart mid
        # burn, or a reload on a big coal bed) the low would be elevated and the rise might never
        # come. 90% of recorded post-reload lows were below 164C; reloads with a low above 150C
        # needed a rescue no more often than others and peaked higher.
        self.ignition_low_cap = setting(ignition_low_cap, "TT_IGNITION_LOW_CAP", 150)
        # Partial close (vent fraction) when ignition completes, before the target is in sight.
        # 0 disables it.
        # About the stove manual's "H" mark, conservatively: closing from wide open once the logs
        # have caught sped up the climb in 79% of the recorded early closes (median close to
        # 0.26; simulation/vent_steps.py). On 18 Oct 2026 the lever's H read 0.35 by the encoder.
        self.early_position = setting(early_position, "TT_EARLY_POSITION", 0.25)
        # A post-reload low below this (degC) is a cold start: there the ignition rise passes
        # long before the fire is established, so require cold_ignition_temp instead
        self.cold_low = setting(cold_low, "TT_COLD_LOW", 60)
        self.cold_ignition_temp = setting(cold_ignition_temp, "TT_COLD_IGNITION_TEMP", 150)
        # A fire that peaked within this of the target (degC) and burned down is in the coal
        # stage, not stalled (18 Oct 2026: a birch load peaking at 274C was reported stalled)
        self.coals_margin = setting(coals_margin, "TT_COALS_MARGIN", 50)
        # Trim mode: the vent may deviate this far (fraction) from the linear closer baseline
        self.trim_band = setting(trim_band, "TT_TRIM_BAND", 0.15)
        # Probe: alternate +/- this around baseline + trim while climbing (each switch moves
        # 2x this, above the actuator's 8-step deadband of about 0.07)
        self.dither = setting(dither, "TT_DITHER", 0.05)
        # Probe period (s), and the response delay (s) skipped before measuring the rise rate
        self.probe_period = setting(probe_period, "TT_PROBE_PERIOD", 180)
        self.probe_delay = setting(probe_delay, "TT_PROBE_DELAY", 60)
        # Move the trim when closing by `dither` changes the rise rate by at least this (C/min)
        self.trim_threshold = setting(trim_threshold, "TT_TRIM_THRESHOLD", 0.5)
        # Earliest time (s) after a reload that a load may be judged unable to reach the target,
        # so a slow-catching load isn't mistaken for a weak one
        self.stall_min_time = setting(stall_min_time, "TT_STALL_MIN_TIME", 900)
        # degC below target that counts as "not at target" (target reached, stall, coals)
        self.margin = setting(margin, "TT_MARGIN", 25)
        # How long (s) a below-target decline or plateau must persist before acting on it
        self.confirm_time = setting(confirm_time, "TT_CONFIRM_TIME", 120)
        # Trend rising slower than this (degC/min) counts as not rising; falling faster as falling
        self.plateau_slope = setting(plateau_slope, "TT_PLATEAU_SLOPE", 0.5)
        # A decline within this long (s) of a closing step is a stall, otherwise coal stage
        self.stall_window = setting(stall_window, "TT_STALL_WINDOW", 1200)
        # Max re-opening (vent fraction) when backing off a stall
        self.backoff_limit = setting(backoff_limit, "TT_BACKOFF_LIMIT", 0.25)
        # Time (s) allowed for the temperature to start rising again after backing off
        self.recovery_time = setting(recovery_time, "TT_RECOVERY_TIME", 600)
        # Back-offs per burn before giving up and declaring the fire stalled
        self.max_rescues = int(setting(max_rescues, "TT_MAX_RESCUES", 2))
        # A stalled fire is regulated again once it is rising and this far (degC) above its low
        # while stalled, e.g. after logs are adjusted or kindling added. Below target the
        # regulate phase holds the vent, so the recovery isn't closed off.
        self.stall_recovery_rise = setting(stall_recovery_rise, "TT_STALL_RECOVERY_RISE", 20)
        # Samples further than this from the filtered value are treated as outliers (e.g. door
        # open exposing the coals) unless they persist for 3 consecutive samples
        self.spike_threshold = setting(spike_threshold, "TT_SPIKE_THRESHOLD", 60)
        # Hold the vent if no valid sample has arrived for this long (s)
        self.stale_timeout = setting(stale_timeout, "TT_STALE_TIMEOUT", 120)
        # With no valid sample for this long (s), close at the linear closer rate like timed_close
        # until samples return, rather than holding e.g. a fully open vent on a fresh load
        self.stale_fallback_time = setting(stale_fallback_time, "TT_STALE_FALLBACK_TIME", 300)
        self.reload_threshold = reload_threshold

        # Signal processing state persists across burns so the filter stays warm
        self.filtered_temp = None  # degC
        self.history = []  # [(time, filtered_temp)] within max(slope_window, trend_window)
        self.last_sample_time = None
        self.spike_count = 0
        self.slope = 0.0  # degC per second, over slope_window
        self.trend = 0.0  # degC per second, over trend_window
        self.predicted_temp = None  # degC

        self._reset_control(0.0)

    def _reset_control(self, position):
        self.phase = PHASE_IGNITION
        self.position = position
        self.pi_position = position
        self.target_reached = False
        self.peak_temp = None
        self.last_error = None
        self.last_control_time = None
        self.close_log = []  # [(time, position before closing)] within stall_window
        self.confirm_timer = 0.0
        self.rescues = 0
        self.rescue_time = None
        self.ignition_low = None
        self.stall_low = None
        self.stale_warned = False
        self.fallback_time = None  # last fallback closing update while samples are missing
        self._rebase(position)

    def _rebase(self, position):
        "Trim mode: continue the baseline from the vent's actual position, with no trim or probe"
        self.baseline = position
        self.trim = 0.0
        self.probe_sign = 0
        self.probe_start = None
        self.probe_rates = []  # [(sign, rise rate C/min)] of the last probe periods

    @property
    def peaked_near_target(self):
        "The fire came within coals_margin of the target: a later decline is the coal stage"
        return self.target_reached or (
            self.peak_temp is not None and self.peak_temp >= self.target - self.coals_margin
        )

    @property
    def fire_stalled(self):
        "True while the fire can't reach or hold the target"
        return self.phase == PHASE_STALLED

    ## VentFunctionABC interface

    def start(self, vent_current_position):
        super().start(vent_current_position)
        self._reset_control(vent_current_position)
        self._log_event("start: reload (regulate %s)" % self.regulate)

    def adjust(self, new_position):
        """Manual override: continue from the new position. Opening (nearly) fully counts as a
        reload and restarts ignition."""
        new_position = max(0.0, min(new_position, 1.0))
        if new_position < self.reload_threshold and self.phase != PHASE_IGNITION:
            self._log_event("manual move to %.3f: treating as reload" % new_position)
            self.start(new_position)
            return
        old_position = self.position
        if new_position > self.position:
            self._log_close(self.time_func(), self.position)
        self.position = new_position
        self.pi_position = new_position
        self._rebase(new_position)
        self.last_error = None  # avoid a proportional kick from the discontinuity
        self.confirm_timer = 0.0
        if self.phase == PHASE_RECOVERING:
            self.phase = PHASE_REGULATE
        self._log_event("manual move %.3f -> %.3f" % (old_position, new_position))

    def inverse(self, position):
        raise NotImplementedError("TemperatureTargetFunction has no time trajectory")

    def save_state(self):
        "Controller and filter state to resume after a soft reboot (see VentFunctionABC)"
        state = super().save_state()

        def rnd(value, digits):
            return None if value is None else round(value, digits)

        state.update({
            "target": self.target,
            "phase": self.phase,
            "position": rnd(self.position, 4),
            "pi_position": rnd(self.pi_position, 4),
            "target_reached": self.target_reached,
            "peak_temp": rnd(self.peak_temp, 2),
            "last_error": rnd(self.last_error, 2),
            "last_control_age": self._age(self.last_control_time),
            "close_log": [[self._age(t), round(p, 4)] for t, p in self.close_log],
            "confirm_timer": self.confirm_timer,
            "rescues": self.rescues,
            "rescue_age": self._age(self.rescue_time),
            "ignition_low": rnd(self.ignition_low, 2),
            "stall_low": rnd(self.stall_low, 2),
            "fallback_age": self._age(self.fallback_time),
            "filtered_temp": rnd(self.filtered_temp, 2),
            "history": [[self._age(t), round(y, 2)] for t, y in self.history],
            "last_sample_age": self._age(self.last_sample_time),
            "slope": self.slope,
            "trend": self.trend,
            "predicted_temp": rnd(self.predicted_temp, 2),
            "baseline": rnd(self.baseline, 4),
            "trim": self.trim,
            "probe_sign": self.probe_sign,
            "probe_age": self._age(self.probe_start),
            "probe_rates": [[s, round(r, 3)] for s, r in self.probe_rates],
        })
        return state

    def restore_state(self, state, downtime):
        super().restore_state(state, downtime)
        self.target = state["target"]
        self.phase = state["phase"]
        self.position = state["position"]
        self.pi_position = state["pi_position"]
        self.target_reached = state["target_reached"]
        self.peak_temp = state["peak_temp"]
        self.last_error = state["last_error"]
        self.last_control_time = self._time(state["last_control_age"])
        self.close_log = [(self._time(age), p) for age, p in state["close_log"]]
        self.confirm_timer = state["confirm_timer"]
        self.rescues = state["rescues"]
        self.rescue_time = self._time(state["rescue_age"])
        self.ignition_low = state["ignition_low"]
        self.stall_low = state["stall_low"]
        self.fallback_time = self._time(state["fallback_age"])
        self.filtered_temp = state["filtered_temp"]
        self.history = [(self._time(age), y) for age, y in state["history"]]
        self.last_sample_time = self._time(state["last_sample_age"])
        self.slope = state["slope"]
        self.trend = state["trend"]
        self.predicted_temp = state["predicted_temp"]
        # Added after the first resumable firmware: a state saved before a deploy that adds a
        # field must still restore, so fields added later default when missing
        self.baseline = state.get("baseline", self.position)
        self.trim = state.get("trim", 0.0)
        self.probe_sign = state.get("probe_sign", 0)
        self.probe_start = self._time(state.get("probe_age"))
        self.probe_rates = [(s, r) for s, r in state.get("probe_rates", [])]
        self.spike_count = 0
        self.stale_warned = False
        self._log_event("resumed after %ds" % int(downtime))

    def get_position(self):
        if self.start_time is None or self.last_sample_time is None:
            return self.position
        now = self.time_func()
        stale_for = now - self.last_sample_time
        if stale_for > self.stale_timeout and not self.stale_warned:
            logger.warning("temp_control: no valid temperature samples, holding vent")
            self.stale_warned = True
        if stale_for > self.stale_fallback_time and self.time_range > 0:
            if self.fallback_time is None:
                self.fallback_time = now
                self._log_event(
                    "no temperature for %ds: closing at the linear rate" % int(stale_for)
                )
            dt = now - self.fallback_time
            self.fallback_time = now
            if self.position < 1.0:
                # A closing step for stall attribution, at most every 10 s to bound the log
                if not self.close_log or now - self.close_log[-1][0] >= 10:
                    self._log_close(now, self.position)
                self.position = min(1.0, self.position + dt / self.time_range)
                self.pi_position = self.position
        return self.position

    ## Measurement and control

    def update_temperature(self, temperature):
        """Feed one temperature sample (degC). Runs the control law if started.

        Returns True if the sample was accepted, False if rejected as an outlier.
        """
        now = self.time_func()
        if not self._filter(now, temperature):
            return False
        self.stale_warned = False
        if self.fallback_time is not None:
            self.fallback_time = None
            self._log_event("temperature samples resumed")
        if self.start_time is not None:
            self._control(now)
        return True

    def _filter(self, now, temperature):
        if self.filtered_temp is None:
            self.filtered_temp = temperature
            self.history = [(now, temperature)]
            self.last_sample_time = now
            self.predicted_temp = temperature
            return True

        if abs(temperature - self.filtered_temp) > self.spike_threshold:
            self.spike_count += 1
            if self.spike_count < 3:
                logger.debug("temp_control: rejected outlier %.1fC", temperature)
                return False
            # Persistent step change: accept it as the new level
            logger.info("temp_control: step change to %.1fC accepted", temperature)
            self.filtered_temp = temperature
            self.history = []
        self.spike_count = 0

        dt = now - self.last_sample_time
        self.last_sample_time = now
        alpha = dt / (self.filter_time + dt) if dt > 0 else 1.0
        self.filtered_temp += alpha * (temperature - self.filtered_temp)

        self.history.append((now, self.filtered_temp))
        keep = max(self.slope_window, self.trend_window)
        while self.history and now - self.history[0][0] > keep:
            self.history.pop(0)
        self.slope = self._least_squares_slope(now - self.slope_window)
        self.trend = self._least_squares_slope(now - self.trend_window)
        self.predicted_temp = self.filtered_temp + self.horizon * self.slope
        return True

    def _least_squares_slope(self, since):
        points = [(t, y) for t, y in self.history if t >= since]
        n = len(points)
        if n < 3:
            return 0.0
        t0 = points[0][0]
        mean_t = sum(t - t0 for t, _ in points) / n
        mean_y = sum(y for _, y in points) / n
        num = 0.0
        den = 0.0
        for t, y in points:
            dt = t - t0 - mean_t
            num += dt * (y - mean_y)
            den += dt * dt
        return num / den if den > 0 else 0.0

    def _log_close(self, now, position_before):
        self.close_log.append((now, position_before))

    def _trim_close_log(self, now):
        while self.close_log and now - self.close_log[0][0] > self.stall_window:
            self.close_log.pop(0)

    def _log_event(self, event):
        """One INFO line per transition or controller action, with the variables behind it.
        t= is seconds since the reload, the same clock as the StoveLink combustion_time recorded
        in the HDF5 files, so log lines can be matched to frames."""
        now = self.time_func()

        def num(value, fmt="%.1f"):
            return "-" if value is None else fmt % value

        elapsed = now - self.start_time if self.start_time is not None else None
        since_close = now - self.close_log[-1][0] if self.close_log else None
        logger.info(
            "temp_control %s | t=%s phase=%s T=%s pred=%s trend=%s/min peak=%s low=%s "
            "target=%.0f reached=%d vent=%.3f since_close=%s rescues=%d",
            event,
            num(elapsed, "%d"),
            self.phase,
            num(self.filtered_temp),
            num(self.predicted_temp),
            num(self.trend * 60, "%+.1f"),
            num(self.peak_temp),
            num(self.ignition_low),
            self.target,
            self.target_reached,
            self.position,
            num(since_close, "%d"),
            self.rescues,
        )

    def _set_phase(self, phase, elapsed, reason):
        old_phase = self.phase
        self.phase = phase
        self.confirm_timer = 0.0
        if phase == PHASE_STALLED:
            self.stall_low = self.filtered_temp
        self._log_event("%s -> %s: %s" % (old_phase, phase, reason))

    def _confirmed(self, condition, dt):
        "Accumulate time while condition holds; True once it has held for confirm_time"
        if condition:
            self.confirm_timer += dt
            return self.confirm_timer >= self.confirm_time
        self.confirm_timer = 0.0
        return False

    def _control(self, now):
        elapsed = now - self.start_time
        dt = 0.0 if self.last_control_time is None else now - self.last_control_time
        # After a gap in samples (camera recovery, a blocked main loop) act like one late sample
        # rather than making up the whole gap's worth of closing and confirmation time at once
        dt = min(dt, MAX_CONTROL_DT)
        self.last_control_time = now
        self._trim_close_log(now)

        temp = self.filtered_temp
        trend_per_min = self.trend * 60
        rising = trend_per_min > self.plateau_slope
        falling = trend_per_min < -self.plateau_slope
        below = temp < self.target - self.margin
        if self.peak_temp is None or temp > self.peak_temp:
            self.peak_temp = temp
        if not below:
            self.target_reached = True
        error = self.predicted_temp - self.target  # positive: too hot, close

        if self.phase == PHASE_IGNITION:
            if self.ignition_low is None or temp < self.ignition_low:
                self.ignition_low = temp
            # Rising too: on a hot stove the mark can be exceeded from the start, and the glass
            # dips right after a reload (open door, cold logs) before the new load catches
            if self.ignition_low < self.cold_low:
                # Cold start: a 40C rise comes with the kindling, long before the logs burn
                caught = rising and temp >= self.cold_ignition_temp
            else:
                caught = rising and (
                    temp >= min(self.ignition_low, self.ignition_low_cap) + self.ignition_rise
                )
            if caught or (not rising and not below):
                self.last_error = error
                self._set_phase(PHASE_REGULATE, elapsed, "logs caught" if caught else "near target")
                # early_position 0 disables it (a fully open vent reads slightly below 0.0)
                if caught and self.early_position > 0 and self.position < self.early_position:
                    old_position = self.position
                    self._log_close(now, self.position)
                    self.position = self.early_position
                    self._log_event("early close %.3f -> %.3f" % (old_position, self.position))
                self.pi_position = self.position
                self._rebase(self.position)
                return
            if self.close_log:
                # Sagging after a manual close: starved, not a weak load
                if self._confirmed(below and falling, dt):
                    self._back_off(now, elapsed)
            elif self._confirmed(below and not rising and elapsed >= self.stall_min_time, dt):
                self._set_phase(PHASE_STALLED, elapsed, "logs haven't caught")
            return

        if self.phase == PHASE_REGULATE and self.regulate == "trim":
            self._regulate_trim(now, dt, elapsed, temp, below, falling, error)
            return

        if self.phase == PHASE_REGULATE:
            # Close-only velocity-form PI: du = Kp * de + Ki * e * dt
            if self.last_error is None:
                self.last_error = error
            du = self.kp * (error - self.last_error) + self.ki * error * dt / 60
            self.last_error = error
            max_step = self.max_close_rate * dt / 60
            du = max(-max_step, min(du, max_step))
            if error < 0:
                # Below target: never close. Without this, the proportional term closes the vent
                # as a young fire's predicted temperature climbs toward the target.
                du = min(du, 0.0)
            self.pi_position = min(1.0, max(self.position - self.noise_band, self.pi_position + du))
            if self.pi_position > self.position:
                # Close only; a closing step is remembered to attribute a later drop to it
                self._log_close(now, self.position)
                self.position = self.pi_position

            if self.close_log:
                # Falling soon after a closing step: starved by the closing
                if self._confirmed(below and falling, dt):
                    self._back_off(now, elapsed)
            elif self._confirmed(below and falling, dt):
                # Peaked and falling with no closing step to blame: burned down, or never got
                # near the target (a slow climber stays here until it peaks)
                if self.peaked_near_target:
                    self._set_phase(PHASE_COALS, elapsed, "coal stage")
                else:
                    self._set_phase(PHASE_STALLED, elapsed, "fire peaked below target")
            return

        if self.phase == PHASE_RECOVERING:
            if rising or not below:
                self.last_error = error
                self.pi_position = self.position
                self.close_log = []
                self._rebase(self.position)
                self._set_phase(PHASE_REGULATE, elapsed, "fire recovered")
            elif now - self.rescue_time >= self.recovery_time:
                # More air didn't help: the fire is fuel-limited, not starved
                if self.peaked_near_target:
                    self._set_phase(PHASE_COALS, elapsed, "no response to back-off: coal stage")
                else:
                    self._set_phase(PHASE_STALLED, elapsed, "fire did not recover after back-off")
            return

        if self.phase == PHASE_STALLED:
            if temp < self.stall_low:
                self.stall_low = temp
            recovering = rising and (
                not below or temp >= self.stall_low + self.stall_recovery_rise
            )
            if self._confirmed(recovering, dt):
                # Logs adjusted or kindling added: regulate holds the vent below target and
                # closes once on course for it. Recent closing steps are kept, so a relapse soon
                # after gets a back-off.
                self.last_error = error
                self.pi_position = self.position
                self._rebase(self.position)
                self._set_phase(PHASE_REGULATE, elapsed, "fire recovering")
                return

        # stalled and coals: close at the linear closer rate
        if self.time_range > 0 and self.position < 1.0:
            if self.phase == PHASE_STALLED and (
                not self.close_log or now - self.close_log[-1][0] >= 10
            ):
                # A closing step for stall attribution, at most every 10 s to bound the log
                self._log_close(now, self.position)
            self.position = min(1.0, self.position + dt / self.time_range)

    def _regulate_trim(self, now, dt, elapsed, temp, below, falling, error):
        """Regulate by the linear closer with a bounded trim:
          baseline  closes at the linear closer rate (time_range per full stroke), from where the
                    vent was when regulating started (after the early close, a manual move or a
                    back-off)
          guard     a short prediction over the target closes faster, up to max_close_rate
          trim      while climbing below the target, the vent alternates +/- dither around
                    baseline + trim every probe_period. The second difference of three periods'
                    rise rates cancels the steady drift of the rise rate and leaves the effect of
                    closing by dither; the trim moves dither toward the faster climb, within
                    +/- trim_band.
        A drop below the target soon after a closing step (guard, probe or trim toward closed,
        or a manual close) gets a back-off, as in predict mode; the baseline's slow creep doesn't
        count as one."""
        creep = dt / self.time_range if self.time_range > 0 else 0.0
        self.baseline = min(1.0, self.baseline + creep)
        if error > 0:
            step = min(self.max_close_rate * dt / 60, self.ki * error * dt / 60)
            self.baseline = min(1.0, self.baseline + step)
        if below and not falling and self.dither > 0:
            self._probe(now)
        else:
            self.probe_sign = 0
            self.probe_start = None
            self.probe_rates = []
        position = min(1.0, max(0.0, self.baseline + self.trim + self.probe_sign * self.dither))
        if position > self.position + creep + 1e-6:
            # A closing step beyond the creep, remembered to attribute a later drop to it
            if not self.close_log or now - self.close_log[-1][0] >= 10:
                self._log_close(now, self.position)
        self.position = position
        self.pi_position = position

        if self.close_log:
            if self._confirmed(below and falling, dt):
                self._back_off(now, elapsed)
        elif self._confirmed(below and falling, dt):
            if self.peaked_near_target:
                self._set_phase(PHASE_COALS, elapsed, "coal stage")
            else:
                self._set_phase(PHASE_STALLED, elapsed, "fire peaked below target")

    def _probe(self, now):
        "One step of the trim probe; see _regulate_trim"
        if self.probe_start is None:
            self.probe_start = now
            self.probe_sign = 1
            self.probe_rates = []
            return
        if now - self.probe_start < self.probe_period:
            return
        rate = self._least_squares_slope(self.probe_start + self.probe_delay) * 60
        self.probe_rates = (self.probe_rates + [(self.probe_sign, rate)])[-3:]
        if len(self.probe_rates) == 3:
            (_, r0), (s1, r1), (_, r2) = self.probe_rates
            # Change in rise rate (C/min) from closing by dither; drift cancels
            effect = s1 * (2 * r1 - r0 - r2) / 4
            if abs(effect) >= self.trim_threshold:
                old = self.trim
                step = self.dither if effect > 0 else -self.dither
                self.trim = max(-self.trim_band, min(self.trim_band, self.trim + step))
                if self.trim != old:
                    self._log_event(
                        "trim %+.2f -> %+.2f: closing %.2f changes the rise by %+.1f/min"
                        % (old, self.trim, self.dither, effect)
                    )
        self.probe_sign = -self.probe_sign
        self.probe_start = now

    def _back_off(self, now, elapsed):
        "Re-open to where the vent was before the recent closing steps, limited to backoff_limit"
        if self.rescues >= self.max_rescues:
            reason = "starved again after %d back-offs" % self.rescues
            self._set_phase(PHASE_STALLED, elapsed, reason)
            return
        old_position = self.position
        earlier_position = self.close_log[0][1]
        self.position = max(earlier_position, self.position - self.backoff_limit)
        self.rescues += 1
        self.rescue_time = now
        self._rebase(self.position)
        reason = "starved by closing, back-off %.3f -> %.3f" % (old_position, self.position)
        self._set_phase(PHASE_RECOVERING, elapsed, reason)
        self.close_log = []

    def get_status(self):
        "Controller diagnostics for Home Assistant and logging"
        return {
            "phase": self.phase if self.start_time is not None else "stopped",
            "fire_stalled": self.start_time is not None and self.fire_stalled,
            "target": self.target,
            "temperature": self.filtered_temp,
            "predicted": self.predicted_temp,
            "slope": self.slope * 60,  # degC per minute
            "position": self.position,
            "rescues": self.rescues,
        }
