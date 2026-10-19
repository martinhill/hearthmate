"""Evaluate strategies on both plant models (v2: slow fuel-dominated, v3: air-limited with fast
flame component) and their parameter variants."""
import json, sys, importlib, numpy as np
import closedloop as cl

def evaluate_model(version, ctl_kwargs=None, strategies=("linear", "temp"), quick=False):
    plant = importlib.import_module(f"plant_v{version}")
    cl.Stove, cl.air = plant.Stove, plant.air
    fit = json.load(open(f"fit_all_v{version}.json"))
    if quick:
        fit["burns"] = fit["burns"][::6]
    plants = cl.variants(fit["global"])
    if quick:
        plants = {k: plants[k] for k in ("fit", "tau_bx2", "K_fx2", "k_ax0.7", "gammax0.5") if k in plants}
    return cl.evaluate(fit, strategies, ctl_kwargs, plants)

if __name__ == "__main__":
    for v in (2, 3):
        print(f"--- model v{v}")
        cl.show(evaluate_model(v, quick="quick" in sys.argv))
