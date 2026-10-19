"""Are the camera faults linked to opening the stove door? The camera is mounted on the door, so
opening it flexes the STEMMA QT cable, and on 18 Oct 2026 the board reset (POWER_ON) while the
door was open to add paper.

A door opening shows in the frames: the view swings away from the fire and the max temperature
drops sharply, then recovers. For every burn, compare how often faults happen within a window of
a door opening with how often they happen elsewhere:
  bad frame   the chess-pattern subpages differ by more than 10C (corrupted read), or max > 600C
  gap         more than 30 s between frames (camera retries, recovery, or a board reset)

    uv run --project ../marimo python door_cable.py
"""
import glob, os
import h5py
import numpy as np
from data import ROOT, ev

WINDOW = 120         # s around a door opening that counts as "near"
DROP = 0.6           # max falls below this fraction of the recent level: door open
RECENT = 6           # frames for the recent level
MIN_LEVEL = 60.0     # C: warm enough for the drop to the room temperature to show


def faults(f):
    with h5py.File(f, "r") as h:
        fr = h["mlx90640_frames"][:]
        t = (h["timestamps_ms"][:].astype(float) - h["timestamps_ms"][0]) / 1000
    if len(t) < 50:
        return None
    mismatch = np.abs(fr[:, ev].mean(1) - fr[:, ~ev].mean(1))
    mx = fr.max(axis=(1, 2)).astype(float)
    bad = (mismatch > 10) | (mx > 600)
    good_mx = np.where(bad, np.nan, mx)
    # Door openings: a good frame far below the recent level
    doors = []
    for i in range(RECENT, len(t)):
        recent = good_mx[i - RECENT:i]
        if np.all(np.isnan(recent)) or np.isnan(good_mx[i]):
            continue
        level = np.nanmedian(recent)
        if level >= MIN_LEVEL and good_mx[i] < min(DROP * level, level - 30):
            if not doors or t[i] - doors[-1] > 300:
                doors.append(t[i])
    gaps = t[1:][np.diff(t) > 30]
    # Reload gestures (vent fully reopened after being closed): the door is opened to load
    vent = h5py.File(f, "r")["vent_positions"][:]
    reloads = t[1:][(vent[1:] > 90) & (vent[:-1] < 20)]
    return t, bad, gaps, np.array(doors), reloads


def compare(name, t, bad, gaps, events, window, acc):
    near = np.zeros(len(t), bool)
    for e in events:
        near |= np.abs(t - e) <= window
    # Frame spacing; the uptime clock restarts with the board, so drop negative and long steps
    dt = np.clip(np.diff(t, prepend=t[0]), 0, 60)
    acc[name]["frames_near"] += near.sum()
    acc[name]["frames_far"] += (~near).sum()
    acc[name]["bad_near"] += (bad & near).sum()
    acc[name]["bad_far"] += (bad & ~near).sum()
    acc[name]["time_near"] += dt[near].sum()
    acc[name]["time_far"] += dt[~near].sum()
    gap_near = np.array([np.any(np.abs(events - g) <= window) for g in gaps], bool) \
        if len(events) else np.zeros(len(gaps), bool)
    acc[name]["gaps_near"] += gap_near.sum()
    acc[name]["gaps_far"] += (~gap_near).sum()
    acc[name]["events"] += len(events)


def main():
    files = [f for f in sorted(glob.glob(ROOT + "20*/*/*.h5")) if "part" not in f]
    keys = ["frames_near", "frames_far", "bad_near", "bad_far", "time_near", "time_far",
            "gaps_near", "gaps_far", "events"]
    acc = {name: dict.fromkeys(keys, 0.0) for name in ("door opening", "reload")}
    for f in files:
        try:
            r = faults(f)
        except (OSError, KeyError):
            continue
        if r is None:
            continue
        t, bad, gaps, doors, reloads = r
        compare("door opening", t, bad, gaps, doors, WINDOW, acc)
        compare("reload", t, bad, gaps, reloads, 300, acc)
    print(f"{len(files)} files")
    for name, a in acc.items():
        window = WINDOW if name == "door opening" else 300
        bn = a["bad_near"] / max(a["frames_near"], 1)
        bf = a["bad_far"] / max(a["frames_far"], 1)
        gn = a["gaps_near"] / max(a["time_near"] / 3600, 1e-9)
        gf = a["gaps_far"] / max(a["time_far"] / 3600, 1e-9)
        print(f"{name}s: {int(a['events'])}, within +/-{window} s: "
              f"{100 * a['frames_near'] / (a['frames_near'] + a['frames_far']):.1f}% of frames")
        print(f"  bad frames  near {100 * bn:.2f}% ({int(a['bad_near'])}), elsewhere {100 * bf:.3f}%"
              f" ({int(a['bad_far'])}): {bn / max(bf, 1e-9):.1f}x")
        print(f"  gaps > 30 s near {gn:.2f}/h ({int(a['gaps_near'])}), elsewhere {gf:.2f}/h"
              f" ({int(a['gaps_far'])}): {gn / max(gf, 1e-9):.1f}x")


if __name__ == "__main__":
    main()
