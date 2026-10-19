"""Plot simulated linear vs temperature-target trajectories for selected burns."""
import json, sys, importlib, numpy as np, matplotlib
matplotlib.use('Agg'); import matplotlib.pyplot as plt
import closedloop as cl
def plot(out, ctl_kwargs=None, version=3, idx=(0, 10, 20, 35, 50, 70, 80, 89)):
    plant = importlib.import_module(f"plant_v{version}"); cl.Stove, cl.air = plant.Stove, plant.air
    fit = json.load(open(f"fit_all_v{version}.json")); g = fit["global"]
    fig, axs = plt.subplots(2, 4, figsize=(20, 8))
    for ax, i in zip(axs.flat, idx):
        b = fit["burns"][i]
        for strat, c in (("linear", "gray"), ("temp", "r")):
            L = cl.run(g, b, strat, ctl_kwargs=ctl_kwargs)
            t = np.array([r[0] for r in L]) / 60
            ax.plot(t, [r[1] for r in L], c, label=strat)
            a2 = ax.twinx() if strat == "linear" else a2
            a2.plot(t, [1 - r[2] for r in L], c, ls=":", lw=1); a2.set_ylim(0, 1.05)
        ax.axhline(300, color="k", lw=.5); ax.set_title(f"{b['name']} m0={b['m0']:.2f}")
    axs[0, 0].legend(); plt.tight_layout(); plt.savefig(out, dpi=55)
if __name__ == "__main__":
    plot(sys.argv[1], version=int(sys.argv[2]))
