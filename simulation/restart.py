"""Soft reboots mid-burn: save the controller state to a (fake) retained MQTT message, rebuild the
controllers as code.py does at boot, restore, and continue. Each restarted burn is compared with
the same burn run straight through, with the same pause in control at the restart time, so any
difference comes from the save/restore itself.

The device clock (time.time) keeps running through a soft reboot; the controllers' monotonic
clock restarts at zero, which exercises re-anchoring every saved time.

    uv run --project ../marimo python restart.py          # all checks, summary
    uv run --project ../marimo python restart.py -v       # also each restart
"""
import os, sys, json, importlib, types
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import logging; logging.basicConfig(level=logging.WARNING, format="%(message)s")
from airvent import Vent
from vent_closer import VentCloser, LinearVentFunction
from temperature_target import TemperatureTargetFunction
from state_machine import StateMachine, State
from burn_state import BurnStateStore

STEP_UNITS = 4096 / 200
DT = 10            # s between camera samples and control updates
DOWNTIME = 15      # s from the save to the restored code running again
VERBOSE = "-v" in sys.argv


class FakeHardware:
    def __init__(self, open_position, closed_position=1225 + 4096):
        self.angle = float(open_position)  # unwrapped encoder units
        # End stops just past each end of the travel, as on the stove
        travel = closed_position - open_position
        self.limits = (open_position - 0.01 * travel, closed_position + 0.01 * travel)
        self.motor = types.SimpleNamespace(release=lambda: None)
        self.step_params = {"min_delay": 0.05}
        self.vent = None
    def read_raw_angle(self): return int(self.angle) % 4096
    def close_vent(self, steps, delay=0.05): self._move(+steps)
    def open_vent(self, steps, delay=0.05): self._move(-steps)
    def _move(self, steps):
        # quarter-revolution chunks so the encoder tracking sees every zero crossing
        while steps:
            chunk = max(-40, min(40, steps)); steps -= chunk
            self.angle = max(self.limits[0], min(self.limits[1], self.angle + chunk * STEP_UNITS))
            self.vent.update_from_hardware(self.read_raw_angle())
    def __getattr__(self, name):  # pixels, LEDs
        return lambda *a, **k: None


class FakeBroker:
    "Retained messages only; loop() delivers the retained message of a subscribed topic"
    def __init__(self): self.retained = {}; self.subscribed = set(); self.handler = None
    def publish(self, topic, payload, retain=False):
        if retain:
            if payload: self.retained[topic] = payload
            else: self.retained.pop(topic, None)
    def subscribe(self, topic): self.subscribed.add(topic)
    def unsubscribe(self, topic): self.subscribed.discard(topic)
    def loop(self, timeout=0.1):
        for topic in self.subscribed:
            if topic in self.retained and self.handler: self.handler(self.retained[topic])


class Idle(State):
    def __init__(self): super().__init__("idle")


class Device:
    """What code.py builds at boot: functions, state machine, vent from the encoder, store.
    device_clock keeps running across reboots, mono restarts at zero."""
    TOPIC = "burnie/controller/saved"

    def __init__(self, hw, broker, device_clock, mode):
        self.mono0 = device_clock()
        mono = lambda: device_clock() - self.mono0
        self.closer = LinearVentFunction(1800, time_func=device_clock)
        self.temp = TemperatureTargetFunction(time_range=1800, time_func=mono)
        # The device calibration: under one revolution of travel, across one zero crossing
        self.vent = Vent(open_position=2950, closed_position=1225, num_zero_crossings=1)
        hw.vent = self.vent
        self.vent.update_from_hardware(hw.read_raw_angle())
        self.machine = StateMachine()
        self.machine.data.update(hardware=hw, vent=self.vent, mqtt_client=None)
        self.machine.add_state(Idle())
        self.machine.add_state(VentCloser(self.closer, name="timed_close"))
        self.machine.add_state(VentCloser(self.temp, name="temp_control"))
        self.store = BurnStateStore(
            broker, self.TOPIC, {"timed_close": self.closer, "temp_control": self.temp}, hw,
            clock=device_clock,
        )
        broker.handler = self.store.on_message
        self.mode = mode

    def function(self):
        return self.temp if self.mode == "temp_control" else self.closer


class Clock:
    def __init__(self): self.t = 0.0
    def __call__(self): return self.t


def run(burn, g, plant, mode, restart_at=None, pause_at=None, reason="AUTO_RELOAD", hours=4,
        kindling=None):
    """One burn. restart_at: soft reboot at that time; pause_at: same pause without a reboot.
    Returns the vent trajectory, phase changes, combustion times and the resume decision."""
    clock = Clock()
    broker = FakeBroker()
    hw = FakeHardware(2950)
    dev = Device(hw, broker, clock, mode)
    dev.machine.set_state(mode)
    stove = plant.Stove(g, burn["m0"], burn["B0"], burn["T0"])
    trajectory, phases, combustion = [], [], []
    decision = None
    state_at_resume = None
    pause = restart_at if restart_at is not None else pause_at
    while clock.t < hours * 3600:
        if kindling is not None and clock.t == kindling * 60:
            # As the driver's --kindling: logs adjusted or kindling added to a stalled fire
            stove.m += 0.25
            stove.B += 0.4
        if pause is not None and pause <= clock.t < pause + DOWNTIME:
            # Control paused (both runs); the restarted run saves when the pause starts
            if restart_at is not None and decision is None and clock.t == pause:
                # As reload_device() and the main loop's finally do before a reload
                dev.store.save(dev.machine, 1234)
                json.loads(broker.retained[Device.TOPIC])  # serializable
            stove.step(min(max(dev.vent.get_position(), 0), 1))
            clock.t += DT
            continue
        if restart_at is not None and decision is None and clock.t >= restart_at:
            # The new code boots: rebuild everything, fetch, check, restore
            dev = Device(hw, broker, clock, mode)
            saved = dev.store.fetch(timeout=0)
            ok, why = dev.store.check(saved, reason, dev.vent.get_position())
            decision = (ok, why)
            if ok:
                dev.store.restore(dev.machine, saved)
            else:
                dev.machine.set_state("idle")
                dev.store.clear()
            if VERBOSE:
                print(f"      restart at {restart_at/60:5.1f} min: {'resume' if ok else 'idle'} - {why}")
        if pause is not None and state_at_resume is None and clock.t >= pause + DOWNTIME:
            # Controller state when control resumes, before the first update
            state_at_resume = dev.function().save_state()
        dev.temp.update_temperature(stove.T)
        dev.machine.update()
        dev.store.maybe_save(dev.machine, 0)
        func = dev.function()
        if getattr(func, "phase", None) and (not phases or phases[-1][1] != func.phase):
            phases.append((clock.t, func.phase))
        pos = dev.vent.get_position()
        trajectory.append((clock.t, pos, dev.machine.current_state))
        combustion.append((clock.t, func.get_elapsed_time()))
        stove.step(min(max(pos, 0), 1))
        clock.t += DT
    return trajectory, phases, combustion, decision, state_at_resume


# The restored vent may settle up to one move deadband (Monitoring.min_steps, 8 motor steps)
# from where the straight run's vent ends up: a straight run paused mid-move finishes the move to
# its old target, the restored one starts from Monitoring with the current target. The stove's
# response to that difference adds a little.
VENT_TOLERANCE = 0.12
PHASE_TIME_TOLERANCE = 60  # s


def state_diff(a, b, path=""):
    "Differences between two save_state() dicts beyond JSON rounding, as a list of paths"
    if isinstance(a, dict):
        return [d for k in a for d in state_diff(a[k], b.get(k), path + "." + k)]
    if isinstance(a, list):
        if len(a) != len(b):
            return [path + " length"]
        return [d for x, y in zip(a, b) for d in state_diff(x, y, path + "[]")]
    if isinstance(a, float) and isinstance(b, (int, float)):
        return [] if abs(a - b) <= 0.01 + 1e-3 * abs(a) else [path]
    return [] if a == b else [path]


def compare(ref, got):
    """Restored state vs the straight run's at the same moment, then the burn: largest vent
    difference, phases (sequence and times), burn time continuity"""
    restored = state_diff(ref[4], got[4])
    worst = max(abs(a[1] - b[1]) for a, b in zip(ref[0], got[0]))
    phases_ok = [p for _, p in ref[1]] == [p for _, p in got[1]] and all(
        abs(a[0] - b[0]) <= PHASE_TIME_TOLERANCE for a, b in zip(ref[1], got[1])
    )
    burn_time_ok = all(abs(a[1] - b[1]) < 1.0 for a, b in zip(ref[2], got[2]))
    return restored, worst, phases_ok, burn_time_ok


def main():
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    failures = 0
    checks = 0
    worst_all = 0.0
    # (model, burn, load, kindling minute): full loads, plus a small load that stalls, the same
    # with kindling added after the stall, and a load that needs a back-off
    scenarios = [(m, b, 1.0, None) for m in (2, 3) for b in (5, 40, 70)]
    scenarios += [(3, 40, 0.4, None), (3, 40, 0.4, 95), (3, 8, 0.6, None)]
    for model, burn_index, load, kindling in scenarios:
        plant = importlib.import_module(f"plant_v{model}")
        fit = json.load(open(f"fit_all_v{model}.json"))
        g = fit["global"]
        burn = dict(fit["burns"][burn_index])
        burn["m0"] *= load
        modes = ("temp_control", "timed_close") if load == 1.0 else ("temp_control",)
        for mode in modes:
            straight = run(burn, g, plant, mode, kindling=kindling)
            # Restart 1 and 3 minutes into each phase, and at fixed times
            times = [t + d for t, _ in straight[1] for d in (60, 180)] + [600, 3600, 7200, 10800]
            times = sorted({int(t // DT) * DT for t in times if t < 4 * 3600 - 600})
            extra = f" load x{load}" + (f" kindling {kindling} min" if kindling else "")
            print(f"v{model} burn {burn_index:2d}{extra} {mode:12s} phases: "
                  + " ".join(p for _, p in straight[1]))
            if True:
                for t in times:
                    ref = run(burn, g, plant, mode, pause_at=t, kindling=kindling)
                    got = run(burn, g, plant, mode, restart_at=t, kindling=kindling)
                    restored, worst, phases_ok, burn_time_ok = compare(ref, got)
                    ok = (got[3][0] and not restored and worst < VENT_TOLERANCE and phases_ok
                          and burn_time_ok)
                    checks += 1
                    worst_all = max(worst_all, worst)
                    if not ok:
                        failures += 1
                        print(f"   FAIL restart at {t/60:5.1f} min: {got[3]}, state {restored or 'ok'}, "
                              f"vent diff {worst:.4f}, phases "
                              f"{'ok' if phases_ok else str(ref[1][:6]) + ' vs ' + str(got[1][:6])}, "
                              f"burn time {'ok' if burn_time_ok else 'reset'}")
                    elif VERBOSE:
                        print(f"   ok   restart at {t/60:5.1f} min: vent diff {worst:.4f}")

    # Cases that must start in idle
    plant = importlib.import_module("plant_v3")
    fit = json.load(open("fit_all_v3.json"))
    g, burn = fit["global"], fit["burns"][40]
    for reason, expect in (("STARTUP", "hard reset"),):
        got = run(burn, g, plant, "temp_control", restart_at=1800, reason=reason, hours=1)
        checks += 1
        ok = not got[3][0] and expect in got[3][1] and got[0][-1][2] == "idle"
        failures += not ok
        print(f"{'ok  ' if ok else 'FAIL'} {reason}: {got[3][1]} -> {got[0][-1][2]}")

    # Old state, clock behind (hard reset that kept the run reason), vent moved: check() alone
    clock = Clock(); clock.t = 5000
    broker = FakeBroker(); hw = FakeHardware(2950)
    dev = Device(hw, broker, clock, "temp_control")
    dev.machine.set_state("temp_control")
    dev.store.save(dev.machine, 1)
    saved = json.loads(broker.retained[Device.TOPIC])
    cases = [
        ("too old", 5000 + 301, 0.0, False, "old"),
        ("clock behind", 20, 0.0, False, "hard reset"),
        ("vent moved", 5010, 0.3, False, "vent moved"),
        ("fresh", 5010, 0.02, True, "temp_control after 10s"),
    ]
    for name, now, vent, expect_ok, expect in cases:
        clock.t = now
        ok, why = dev.store.check(saved, "SUPERVISOR_RELOAD", vent)
        good = ok == expect_ok and expect in why
        checks += 1
        failures += not good
        print(f"{'ok  ' if good else 'FAIL'} {name}: {'resume' if ok else 'idle'} - {why}")
    clock.t = 5010
    print(f"saved state size: {len(broker.retained[Device.TOPIC])} bytes")

    print(f"largest vent difference after a resume: {worst_all:.3f} (tolerance {VENT_TOLERANCE})")
    print(f"{checks - failures}/{checks} restart checks passed")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
