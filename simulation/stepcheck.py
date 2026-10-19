"""Does a stove model respond to vent steps like the stove? Replays each fitted burn's recorded
vent positions through the model and measures the change in rise rate across every isolated vent
step, on the measured and on the simulated temperature, as vent_steps.py does.

A controller that acts on short-term responses (TT_REGULATE = "trim") can only be judged in
simulation against a model that passes this check.

    uv run --project ../marimo python stepcheck.py fit_all_v3.json fit_all_v4.json ...
(needs vent_steps_cache.npz: run vent_steps.py first)
"""
import importlib, json, os, sys
import numpy as np
import vent_steps as vs


def check(fit_file):
    version = fit_file.split("_v")[-1][0]
    plant = importlib.import_module(f"plant_v{version}")
    fit = json.load(open(fit_file))
    cache = np.load(vs.CACHE, allow_pickle=True)["burns"].item()
    by_name = {os.path.basename(f): v for f, v in cache.items()}
    obs, sim = [], []
    for burn in fit["burns"]:
        if burn["name"] not in by_name:
            continue
        t, u, T = by_name[burn["name"]]
        tt = np.arange(0, t[-1], 10.0)
        uu, TT = np.interp(tt, t, u), np.interp(tt, t, T)
        Ts = plant.simulate(fit["global"], uu, burn["m0"], burn["B0"], TT[0])
        steps = vs.jumps(tt, uu)
        times = [tt[i] for i in steps]
        for i in steps:
            t0 = tt[i]
            if any(x != t0 and -vs.QUIET_BEFORE < x - t0 < vs.QUIET_AFTER for x in times):
                continue
            k0, k1 = max(0, i - 3), min(len(tt) - 1, i + 6)
            measured = vs.rate(tt, TT, t0, vs.POST) - vs.rate(tt, TT, t0, vs.PRE)
            simulated = vs.rate(tt, Ts, t0, vs.POST) - vs.rate(tt, Ts, t0, vs.PRE)
            if not (np.isnan(measured) or np.isnan(simulated)):
                obs.append((TT[k0], uu[k0], uu[k1] - uu[k0], measured))
                sim.append(simulated)
    obs, sim = np.array(obs), np.array(sim)
    rms = np.median([b["rms"] for b in fit["burns"]])
    print(f"{fit_file}: median burn RMS {rms:.1f} C, {len(sim)} steps, correlation of simulated "
          f"vs measured change in rise rate {np.corrcoef(obs[:, 3], sim)[0, 1]:+.2f}")
    for name, sel in (
        ("close, glass < 200C, from mostly open", (obs[:, 2] > 0) & (obs[:, 0] < 200) & (obs[:, 1] < 0.4)),
        ("close, glass >= 200C", (obs[:, 2] > 0) & (obs[:, 0] >= 200)),
        ("open, from mostly closed", (obs[:, 2] < 0) & (obs[:, 1] >= 0.4)),
    ):
        if sel.sum():
            print(f"    {name:38s} n={sel.sum():3d}  measured {np.median(obs[sel, 3]):+5.1f}"
                  f"  simulated {np.median(sim[sel]):+5.1f} C/min (medians)")


if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    for f in sys.argv[1:] or ["fit_all_v3.json", "fit_all_v4.json"]:
        check(f)
