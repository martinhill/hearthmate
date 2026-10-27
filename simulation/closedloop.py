"""Closed-loop simulation of the vent strategies against the fitted stove model."""
import json, os, sys, random, numpy as np
# append: firmware/logging.py shadows stdlib
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'firmware'))
import logging; logging.basicConfig(level=logging.WARNING)
from temperature_target import TemperatureTargetFunction
from plant_v3 import Stove, air  # ensemble.py swaps in other plant versions

DT = 10.0
DEADBAND = 0.03      # Monitoring only moves the vent when the step count exceeds min_steps
DURATION = 5 * 3600


class Clock:
    t = 0.0
    def __call__(self):
        return self.t


def run(params, burn, strategy, target=300, close_time=2700, ctl_kwargs=None, noise=0.5, seed=0,
        delay=60.0):
    rng = random.Random(seed)
    s = Stove(params, burn["m0"], burn["B0"], burn["T0"], DT)
    clock = Clock()
    ctl = None
    if strategy == "temp":
        ctl = TemperatureTargetFunction(target=target, time_range=close_time, time_func=clock,
                                        **(ctl_kwargs or {}))
        ctl.start(0.0)
    u_act = 0.0
    pipeline = [0.0] * int(delay / DT)  # extra transport delay, conservative
    log = []
    while clock.t < DURATION:
        T = s.T
        if strategy == "linear":
            u_cmd = min(1.0, clock.t / close_time)
        elif strategy == "temp":
            ctl.update_temperature(T + rng.gauss(0, noise))
            u_cmd = ctl.get_position()
        # actuator deadband like Monitoring/Closing, but always reach fully closed
        if abs(u_cmd - u_act) >= DEADBAND or u_cmd > 0.999:
            u_act = u_cmd
        pipeline.append(u_act)
        s.step(pipeline.pop(0))
        log.append((clock.t, s.T, u_act, s.m, s.B, ctl.phase if ctl else "", ctl.rescues if ctl else 0))
        clock.t += DT
    return log


def metrics(log, params, target=300, band=15):
    t = np.array([r[0] for r in log]); T = np.array([r[1] for r in log])
    u = np.array([r[2] for r in log]); m = np.array([r[3] for r in log])
    burning = m > 0.05 * m[0]
    a = air(u, params["a0"])
    closed = np.argmax(u > 0.95) if (u > 0.95).any() else len(u) - 1
    phases = [r[5] for r in log]
    coal_i = phases.index("coals") if "coals" in phases else None
    return dict(
        air_per_burn=float(a[burning].sum() * DT / 3600),          # air-hours while fuel remains
        mean_air=float(a[burning].mean()),
        at_target_min=float(((T >= target - band) & burning).sum() * DT / 60),
        over_min=float((T > target + 40).sum() * DT / 60),
        starved_min=float(((T < 200) & (m > 0.3 * m[0])).sum() * DT / 60),
        close_min=float(t[closed] / 60),
        burn_min=float(burning.sum() * DT / 60),
        peak=float(T.max()),
        stalled=float("stalled" in phases),
        coals=float(coal_i is not None),
        fuel_at_coals=float(m[coal_i] / m[0]) if coal_i is not None else float("nan"),
        rescues=float(log[-1][6]),
    )



def variants(g):
    "Plant ensemble around the fit for robustness checks"
    out = {"fit": g}
    for k, f in [("tau_b", 0.5), ("tau_b", 2), ("K_f", 0.5), ("K_f", 2), ("k_a", 0.7), ("k_a", 1.4),
                 ("tau_s", 0.5), ("a0", 0.5), ("gamma", 0.5)]:
        if k in g:
            v = dict(g); v[k] = v[k] * f; out[f"{k}x{f}"] = v
    return out


def evaluate(fit, strategies=("linear", "temp"), ctl_kwargs=None, plants=None, target=300):
    plants = plants or variants(fit["global"])
    table = {}
    for pname, g in plants.items():
        for strat in strategies:
            ms = [metrics(run(g, b, strat, target=target, ctl_kwargs=ctl_kwargs), g, target)
                  for b in fit["burns"]]
            table[(pname, strat)] = {k: float(np.nanmean([m[k] for m in ms])) for k in ms[0]}
    return table


def show(table):
    for (p, s), r in table.items():
        print(f"{p:10s} {s:7s}", "  ".join(f"{k}={v:6.2f}" for k, v in r.items()))


if __name__ == "__main__":
    fit = json.load(open(sys.argv[1] if len(sys.argv) > 1 else "fit_all_v3.json"))
    show(evaluate(fit))
