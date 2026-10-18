"""Evaluate the chosen controller defaults on all burns and plant variants; plot trajectories."""
import json, importlib, random, numpy as np
import closedloop as cl
from ensemble import evaluate_model
from traj import plot
keys = ["mean_air", "at_target_min", "over_min", "starved_min", "close_min", "peak",
        "stalled", "coals", "fuel_at_coals", "rescues"]
for v in (2, 3):
    tab = evaluate_model(v)
    for strat in ("linear", "temp"):
        rows = [r for (p, s), r in tab.items() if s == strat]
        print(f"v{v} {strat:6s}", "  ".join(f"{k}={np.nanmean([r[k] for r in rows]):6.2f}" for k in keys))
    worst = max((p for p, s in tab if s == "temp"), key=lambda p: tab[(p, "temp")]["starved_min"] - tab[(p, "linear")]["starved_min"])
    print(f"   worst-case variant for starving vs linear: {worst}",
          {k: round(tab[(worst, s)][k], 1) for s in ("linear", "temp") for k in ("starved_min",)})
# noise / spike robustness on v3 fit
plant = importlib.import_module("plant_v3"); cl.Stove, cl.air = plant.Stove, plant.air
fit = json.load(open("fit_all_v3.json")); g = fit["global"]
for noise in (0.5, 3.0, 8.0):
    ms = [cl.metrics(cl.run(g, b, "temp", noise=noise), g) for b in fit["burns"][::3]]
    print(f"noise {noise}C:", {k: round(np.mean([m[k] for m in ms]), 2) for k in keys})
plot("final_v3.png", version=3, idx=(5, 15, 25, 40, 55, 65, 75, 85))
plot("final_v2.png", version=2, idx=(5, 15, 25, 40, 55, 65, 75, 85))
