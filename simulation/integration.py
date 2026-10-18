"""Drive the real VentCloser state machine + TemperatureTargetFunction with fake hardware and the
stove model, exactly as code.py does (state update every loop, temperature every 10 s)."""
import os, sys, json, importlib, types
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import logging; logging.basicConfig(level=logging.INFO, format="%(message)s")
from airvent import Vent
from vent_closer import VentCloser
from temperature_target import TemperatureTargetFunction
from state_machine import StateMachine

STEP_UNITS = 4096 / 200

class FakeHardware:
    def __init__(self, vent):
        self.angle = float(vent.open_position)  # unwrapped encoder units
        self.motor = types.SimpleNamespace(release=lambda: None)
    def read_raw_angle(self): return int(self.angle) % 4096
    def close_vent(self, steps, delay=0.05): self._move(+steps)
    def open_vent(self, steps, delay=0.05): self._move(-steps)
    def _move(self, steps):
        # move in quarter-revolution chunks so the encoder tracking sees every zero crossing
        while steps:
            chunk = max(-40, min(40, steps)); steps -= chunk
            self.angle += chunk * STEP_UNITS
            vent.update_from_hardware(self.read_raw_angle())
    def __getattr__(self, name):  # pixels/leds
        return lambda *a, **k: None

class Clock:
    t = 0.0
    def __call__(self): return self.t

plant = importlib.import_module("plant_v3")
fit = json.load(open("fit_all_v3.json")); g = fit["global"]; burn = fit["burns"][int(sys.argv[1]) if len(sys.argv) > 1 else 40]
vent = Vent(open_position=300, closed_position=1200, num_zero_crossings=1)
hw = FakeHardware(vent)
vent.update_from_hardware(hw.read_raw_angle())
clock = Clock()
func = TemperatureTargetFunction(time_func=clock)
machine = StateMachine()
machine.data.update(hardware=hw, vent=vent, mqtt_client=None)
machine.add_state(VentCloser(func, name="temp_control"))
machine.set_state("temp_control")
stove = plant.Stove(g, burn["m0"], burn["B0"], burn["T0"])
moves = 0; last_pos = vent.get_position(); sub = None
while clock.t < 4 * 3600:
    func.update_temperature(stove.T)
    machine.update()
    s = machine.states["temp_control"].machine.current_state
    if s != sub:
        print(f"{clock.t/60:6.1f} min  substate={s:10s} T={stove.T:5.1f} vent={vent.get_position():.3f} phase={func.phase}")
        sub = s
    pos = vent.get_position()
    if abs(pos - last_pos) > 1e-3: moves += 1
    last_pos = pos
    stove.step(min(max(pos, 0), 1))
    clock.t += 10
print(f"done: {moves} vent moves, final vent={vent.get_position():.3f}, phase={func.phase}, substate={sub}")
