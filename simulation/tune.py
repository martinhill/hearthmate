"""Tuning grid search for TemperatureTargetFunction on both plant models."""
import itertools, numpy as np
from ensemble import evaluate_model
base = {v: evaluate_model(v, strategies=("linear",), quick=True) for v in (2, 3)}
def summary(tab):
    keys = ["mean_air", "at_target_min", "over_min", "starved_min", "close_min"]
    return {k: np.mean([r[k] for r in tab.values()]) for k in keys}
lin = {v: summary(base[v]) for v in base}
print("linear", {v: {k: round(x, 2) for k, x in lin[v].items()} for v in lin})
res = []
grid = dict(horizon=[300, 450, 600], max_close_rate=[0.1, 0.2], kp=[0.006, 0.015], ki=[0.003, 0.006], margin=[20, 30])
for combo in itertools.product(*grid.values()):
    kw = dict(zip(grid, combo))
    s = {v: summary(evaluate_model(v, ctl_kwargs=kw, strategies=("temp",), quick=True)) for v in (2, 3)}
    res.append((kw, s))
def score(s):  # less air is better; must not lose time at target or starve more than linear
    return sum(s[v]["mean_air"] - lin[v]["mean_air"]
               + 0.01 * max(0, lin[v]["at_target_min"] - s[v]["at_target_min"])
               + 0.01 * max(0, s[v]["starved_min"] - lin[v]["starved_min"]) for v in s)
res.sort(key=lambda r: score(r[1]))
for kw, s in res[:8]:
    print(round(score(s), 3), kw, {v: {k: round(x, 2) for k, x in s[v].items()} for v in s})
