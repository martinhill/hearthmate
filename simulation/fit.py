"""Fit a stove model (plant_v2 or plant_v3) to all suitable recorded burns.

Usage: uv run --project ../marimo --with scipy python fit.py [2|3]   (takes ~6 minutes)
"""
import json, os, sys, numpy as np
from scipy.optimize import least_squares
from scipy.sparse import lil_matrix
from data import linear_burns, reopen_burns
import importlib
VERSION = int(sys.argv[1]) if len(sys.argv) > 1 else 3
plant = importlib.import_module(f"plant_v{VERSION}")
simulate, NAMES, DEFAULT = plant.simulate, plant.NAMES, plant.DEFAULT

burns = linear_burns() + reopen_burns()
print("burns:", len(burns), flush=True)
SUB = 3
R = []
for name, t, u, m in burns:
    tt = np.arange(0, t[-1], 10.0)
    R.append((name, np.interp(tt, t, u), np.interp(tt, t, m)))
NB, G = len(R), len(NAMES)
lo_g, hi_g = plant.LO, plant.HI

def unpack(x):
    return dict(zip(NAMES, x[:G])), x[G:].reshape(NB, 2)

# FIT_RATE_WEIGHT > 0 also fits the 1-minute rise rate (degC per minute, times the weight), so
# the model has to follow the fast response to vent steps, not only the slow temperature level
RATE_WEIGHT = float(os.environ.get("FIT_RATE_WEIGHT", "0"))
RATE_STEP = 6  # samples of 10 s per rate interval

def resid(x):
    g, per = unpack(x)
    out = []
    for (name, u, m), (m0, B0) in zip(R, per):
        sim = simulate(g, u, m0, B0, m[0])
        out.append((sim - m)[::SUB])
        if RATE_WEIGHT > 0:
            out.append(RATE_WEIGHT * (np.diff(sim[::RATE_STEP]) - np.diff(m[::RATE_STEP])))
    return np.concatenate(out)

lens = [len(m[::SUB]) + (len(m[::RATE_STEP]) - 1 if RATE_WEIGHT > 0 else 0) for _, _, m in R]
J = lil_matrix((sum(lens), G + 2 * NB), dtype=int)
row = 0
for i, L in enumerate(lens):
    J[row:row + L, :G] = 1
    J[row:row + L, G + 2 * i:G + 2 * i + 2] = 1
    row += L
x0 = np.array([DEFAULT[n] for n in NAMES] + [1.0, 0.3] * NB)
lb = np.array([lo_g[n] for n in NAMES] + [0.05, 0.0] * NB)
ub = np.array([hi_g[n] for n in NAMES] + [10.0, 5.0] * NB)
sol = least_squares(resid, np.clip(x0, lb, ub), bounds=(lb, ub), jac_sparsity=J, x_scale='jac',
                    loss='soft_l1', f_scale=20.0, max_nfev=120, verbose=1)
g, per = unpack(sol.x)
r = resid(sol.x)
print("RMS error C:", np.sqrt(np.mean(r**2)).round(1), " median abs:", np.median(np.abs(r)).round(1))
print(json.dumps({k: round(float(v), 5) for k, v in g.items()}))
out = os.environ.get("FIT_OUT", f"fit_all_v{VERSION}.json")
json.dump({"global": {k: float(v) for k, v in g.items()},
           "burns": [{"name": n, "m0": float(a), "B0": float(b), "T0": float(m[0]),
                      "rms": float(np.sqrt(np.mean((simulate(g, u, a, b, m[0]) - m) ** 2)))}
                     for (n, u, m), (a, b) in zip(R, per)]}, open(out, "w"), indent=1)
