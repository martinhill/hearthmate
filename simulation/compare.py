"""Compare vent strategies in closed loop against a fitted stove model (default v4):
linear (timed_close), predict (temp_control's original regulate) and trim (regulate by the linear
closer with a probed, bounded trim). Recorded burns as fitted, plus cold starts.

    uv run --project ../marimo python compare.py [fit_all_v4.json] [plant version]
"""
import importlib, json, os, sys
import numpy as np
import closedloop

fit_file = sys.argv[1] if len(sys.argv) > 1 else "fit_all_v4.json"
version = sys.argv[2] if len(sys.argv) > 2 else fit_file.split("_v")[-1].split(".")[0]
plant = importlib.import_module(f"plant_v{version}")
closedloop.Stove, closedloop.air = plant.Stove, plant.air

fit = json.load(open(fit_file))
g = fit["global"]
STRATEGIES = {
    "linear": ("linear", None),
    "predict": ("temp", {"regulate": "predict"}),
    "trim": ("temp", {"regulate": "trim"}),
}
KEYS = ["mean_air", "at_target_min", "over_min", "starved_min", "close_min", "peak", "stalled",
        "coals", "rescues"]


def table(burns, plants):
    rows = {}
    for pname, params in plants.items():
        for sname, (strategy, kwargs) in STRATEGIES.items():
            ms = [closedloop.metrics(closedloop.run(params, b, strategy, ctl_kwargs=kwargs), params)
                  for b in burns]
            rows[(pname, sname)] = {k: float(np.nanmean([m[k] for m in ms])) for k in KEYS}
    return rows


def show(title, rows):
    print(f"\n{title}")
    print(f"{'plant':10s} {'strategy':8s} " + " ".join(f"{k:>13s}" for k in KEYS))
    for (p, s), r in rows.items():
        print(f"{p:10s} {s:8s} " + " ".join(f"{r[k]:13.2f}" for k in KEYS))


plants = closedloop.variants(g)
for extra in ("d", "e"):
    if extra in g:
        for f in (0.5, 2.0):
            v = dict(g); v[extra] = g[extra] * f; plants[f"{extra}x{f}"] = v
burns = fit["burns"]
show(f"Recorded burns ({len(burns)}), fit and variants, plant v{version}", table(burns, plants))

# Cold starts: a cold stove and a fresh load, the case the fitted reloads hardly cover
cold = [dict(b, T0=25.0, B0=0.05) for b in burns[::6]]
show(f"Cold starts ({len(cold)} loads from 25C), fit only", table(cold, {"fit": g}))
