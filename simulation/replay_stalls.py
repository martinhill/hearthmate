"""Replay recorded burns through the stall / coal-stage classifier of TemperatureTargetFunction.

The vent followed the linear closer (and your manual moves) in these recordings, so the
controller's own PI is disabled (kp = ki = 0) and the recorded vent position is imposed each
sample. Closing steps of the recorded vent are logged as if the controller had made them. Two
checks:
  - manual rescues: does the controller detect the stall (back off) before or when you
    re-opened the vent by hand?
  - burns left alone after the peak: is the decline classified as coal stage (or as
    "can't reach target" for loads that never got near it), without false back-offs?
"""
import glob, os, sys
import numpy as np, h5py
from data import ROOT, load
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'firmware'))
import logging; logging.basicConfig(level=logging.WARNING)
from temperature_target import TemperatureTargetFunction, PHASE_IGNITION, PHASE_REGULATE


class Clock:
    t = 0.0
    def __call__(self):
        return self.t


def replay(t, u, T, stop, fair=False):
    """Run the classifier until index stop; returns (phase change time or None, new phase).
    fair: only log recorded closing steps the controller itself would have made, i.e. while the
    predicted temperature was at or above target."""
    clock = Clock()
    ctl = TemperatureTargetFunction(time_func=clock, kp=0, ki=0, time_range=2700)
    ctl.start(u[0])
    for i in range(stop):
        clock.t = t[i]
        if i and u[i] > u[i - 1] + 0.005 and (not fair or ctl.predicted_temp >= ctl.target):
            ctl._log_close(t[i], u[i - 1])
        ctl.position = u[i]
        ctl.update_temperature(T[i])
        if ctl.phase not in (PHASE_IGNITION, PHASE_REGULATE):
            return t[i], ctl.phase
    return None, None


FAIR = "fair" in sys.argv
rescues, coals = [], []
for f in sorted(glob.glob(ROOT + '20*/*/*.h5')):
    try:
        with h5py.File(f, 'r') as h: n = h['sequence_ids'].shape[0]
    except OSError:
        continue
    if not (300 < n < 3000): continue
    t, u, T = load(f)
    if u[0] > 0.05: continue
    end = n - 1
    while end > 0 and u[end] < 0.9: end -= 1
    reopen = [i for i in range(6, end - 2) if u[i] - u[i + 2] > 0.08 and t[i] > 300]
    if reopen:
        i = reopen[0]
        pre = (t > t[i] - 300) & (t <= t[i])
        sagging = T[i] < 285 and np.polyfit(t[pre], T[pre], 1)[0] * 60 < -0.3
        if sagging:
            when, phase = replay(t, u, T, i + 1, FAIR)
            moved = [k for k in range(1, i + 1) if u[k] > u[k - 1] + 0.005]
            since = (t[i] - t[moved[-1]]) / 60 if moved else np.nan
            pre5 = (t > t[i] - 300) & (t <= t[i])
            rescues.append((os.path.basename(f), t[i] / 60, None if when is None else when / 60, phase,
                            T[i], np.polyfit(t[pre5], T[pre5], 1)[0] * 60, since, T[:i].max()))
    else:
        when, phase = replay(t, u, T, end, FAIR)
        coals.append((os.path.basename(f), T[:end].max(), None if when is None else when / 60, phase))

print(f"Manual rescues of a sagging fire: {len(rescues)}")
lead = [r[1] - r[2] for r in rescues if r[2] is not None]
by_phase = {}
for r in rescues:
    by_phase[r[3]] = by_phase.get(r[3], 0) + 1
print("  controller reaction before your re-open:", by_phase)
if lead:
    print("  minutes before you re-opened: median %.1f, 25-75%% %s" % (np.median(lead), np.percentile(lead, [25, 75]).round(1)))
if "detail" in sys.argv:
    for r in rescues:
        print("   %s re-opened at %3.0f min: T=%3.0f trend=%5.1f C/min, %4.1f min after a close, peak %3.0f -> %s"
              % (r[0], r[1], r[4], r[5], r[6], r[7], r[3]))
print(f"Burns left alone after the peak: {len(coals)}")
by_phase = {}
for r in coals:
    by_phase[r[3]] = by_phase.get(r[3], 0) + 1
print("  classification of the decline:", by_phase)
for r in coals if "detail" in sys.argv else []:
    if r[3] == "recovering":
        print("   back-off in", r[0], "peak %.0f at %.0f min" % (r[1], r[2]))
