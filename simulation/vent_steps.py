"""How the glass temperature responds to a step in the vent position, across the recorded burns.

For every isolated vent step (manual moves, overrides, back-offs; not the slow linear closing),
compare the rise rate of the max temperature in the 3 minutes before with minutes 1-4 after.
The rate also changes by itself as a burn develops, so each step is compared with what the rate
did without any step, at a similar temperature and rise rate: the difference is the effect of the
move. Positive = the move made the temperature climb faster (or fall slower).

    uv run --project ../marimo python vent_steps.py [extra.h5 ...]
"""
import glob, os, sys
import numpy as np
from data import ROOT, load

CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vent_steps_cache.npz")
STEP = 0.05          # vent fraction moved within JUMP_TIME to count as a step
JUMP_TIME = 30       # s
QUIET_BEFORE = 180   # s without another step before
QUIET_AFTER = 300    # s without another step after
PRE = (-180, 0)      # s, window for the rate before
POST = (60, 240)     # s, window for the rate after (past the response delay)


def rate(t, y, t0, window):
    m = (t >= t0 + window[0]) & (t < t0 + window[1])
    if m.sum() < 6:
        return np.nan
    return np.polyfit(t[m] / 60, y[m], 1)[0]  # degC per minute


def smooth(y, n=5):
    "Running median, against single corrupt frames"
    return np.array([np.median(y[max(0, i - n // 2):i + n // 2 + 1]) for i in range(len(y))])


def burns(extra):
    files = sorted(glob.glob(ROOT + "20*/*/*.h5")) + extra
    cached = {}
    if os.path.exists(CACHE):
        z = np.load(CACHE, allow_pickle=True)
        cached = z["burns"].item()
    out = {}
    for f in files:
        if "part" in f:
            continue
        if f in cached:
            out[f] = cached[f]
            continue
        try:
            t, u, mx = load(f)
        except (OSError, KeyError, ValueError):
            continue
        if len(t) < 100:
            continue
        out[f] = (t, u, smooth(mx))
    np.savez(CACHE, burns=np.array(out, dtype=object))
    return out


def jumps(t, u):
    "Times where the vent moved at least STEP within JUMP_TIME, one per move"
    idx = []
    j = 0
    for i in range(len(t)):
        while t[i] - t[j] > JUMP_TIME:
            j += 1
        if abs(u[i] - u[j]) >= STEP and (not idx or t[i] - t[idx[-1]] > JUMP_TIME * 2):
            idx.append(i)
    return idx


def main():
    data = burns(sys.argv[1:])
    steps, quiet = [], []  # (T, pre rate, change in rate, du, u before, minutes into file, file)
    for f, (t, u, T) in data.items():
        jt = [t[i] for i in jumps(t, u)]
        for i in jumps(t, u):
            t0 = t[i]
            k0 = np.searchsorted(t, t0 - JUMP_TIME)
            others = [x for x in jt if x != t0 and -QUIET_BEFORE < x - t0 < QUIET_AFTER]
            if others:
                continue
            k1 = np.searchsorted(t, t0 + 60)
            if k1 >= len(t):
                continue
            pre, post = rate(t, T, t0, PRE), rate(t, T, t0, POST)
            if np.isnan(pre) or np.isnan(post):
                continue
            steps.append((T[k0], pre, post - pre, u[k1] - u[k0], u[k0], t0 / 60, f))
        # The same rate change with no step, every minute
        for t0 in np.arange(t[0] + 240, t[-1] - 300, 60):
            if any(-QUIET_BEFORE - 60 < x - t0 < QUIET_AFTER for x in jt):
                continue
            pre, post = rate(t, T, t0, PRE), rate(t, T, t0, POST)
            if not (np.isnan(pre) or np.isnan(post)):
                k0 = np.searchsorted(t, t0)
                quiet.append((T[k0], pre, post - pre))
    steps = np.array(steps, dtype=object)
    quiet = np.array(quiet, dtype=float)
    print(f"{len(data)} burns, {len(steps)} isolated vent steps, {len(quiet)} no-step baselines")

    def effect(s):
        "Change in rate across the step minus the median change without a step nearby"
        T, pre = float(s[0]), float(s[1])
        m = (np.abs(quiet[:, 0] - T) < 25) & (np.abs(quiet[:, 1] - pre) < 1.5)
        if m.sum() < 10:
            return np.nan, int(m.sum())
        return float(s[2]) - np.median(quiet[m, 2]), int(m.sum())

    rows = []
    for s in steps:
        e, n = effect(s)
        if not np.isnan(e):
            rows.append((float(s[0]), float(s[1]), float(s[3]), float(s[4]), e, s[5], s[6]))
    rows = np.array(rows, dtype=object)
    print(f"{len(rows)} steps with a matched baseline\n")

    def summary(name, sel):
        r = rows[sel]
        if len(r) == 0:
            return
        e = r[:, 4].astype(float)
        print(f"  {name:44s} n={len(r):3d}  effect median {np.median(e):+5.1f} C/min"
              f"  (IQR {np.percentile(e, 25):+5.1f}..{np.percentile(e, 75):+5.1f}),"
              f" faster in {100 * np.mean(e > 0):3.0f}%")

    T, pre, du, u0 = (rows[:, i].astype(float) for i in range(4))
    close, open_ = du > 0, du < 0
    print("Effect of a vent step on the temperature rise rate (degC/min), vs no step:")
    for label, sel in (("closing", close), ("opening", open_)):
        print(f"{label}:")
        summary("all", sel)
        summary("while rising (pre rate > +1)", sel & (pre > 1))
        summary("while level (-1..+1)", sel & (np.abs(pre) <= 1))
        summary("while falling (pre rate < -1)", sel & (pre < -1))
        summary("glass < 200C", sel & (T < 200))
        summary("glass 200-275C", sel & (T >= 200) & (T < 275))
        summary("glass >= 275C", sel & (T >= 275))
        summary("from mostly open (< 0.4 closed)", sel & (u0 < 0.4))
        summary("from mostly closed (>= 0.4 closed)", sel & (u0 >= 0.4))
    np.save(os.path.join(os.path.dirname(CACHE), "vent_steps_rows.npy"), rows, allow_pickle=True)


if __name__ == "__main__":
    main()
