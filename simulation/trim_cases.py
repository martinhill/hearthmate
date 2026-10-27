"""Trim mode on scripted glass temperature traces: when a decline below the target backs off and
when it is the coal stage. The traces are open loop (the vent doesn't change them), which is
enough for the decision logic.

    uv run --project ../marimo python trim_cases.py
"""
import os, sys
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'firmware'))
import logging; logging.basicConfig(level=logging.WARNING, format="%(message)s")
from temperature_target import TemperatureTargetFunction

DT = 10  # s between samples


class Clock:
    t = 0.0
    def __call__(self):
        return self.t


def trace(points):
    "Piecewise linear temperature from [(minute, degC), ...]"
    def temp(t):
        m = t / 60
        for (m0, T0), (m1, T1) in zip(points, points[1:]):
            if m <= m1:
                return T0 + (T1 - T0) * (m - m0) / (m1 - m0)
        return points[-1][1]
    return temp


def run(points, minutes, manual=()):
    """Feed the trace; manual is [(minute, position)] hand moves. Returns the controller and the
    (minute, phase) transitions"""
    clock = Clock()
    ctl = TemperatureTargetFunction(target=300, time_range=2700, time_func=clock, regulate="trim")
    ctl.start(0.0)
    temp, moves, phases = trace(points), list(manual), []
    while clock.t < minutes * 60:
        if moves and clock.t >= moves[0][0] * 60:
            ctl.adjust(moves.pop(0)[1])
        ctl.update_temperature(temp(clock.t))
        if not phases or phases[-1][1] != ctl.phase:
            phases.append((round(clock.t / 60, 1), ctl.phase))
        clock.t += DT
    return ctl, phases


# 24 Oct 2026: cold start, 300C at 19.5 min, the guard closed the vent by 24 min and held it
# closed over the target until the fuel ran down at 3.4C/min
OCT24 = [(0, 22), (7.5, 150), (19.5, 300), (24, 309), (38, 315), (48, 317), (55, 298), (85, 200)]
# A hot reload that overshoots: the guard closes hard, and the fire sags within minutes
OVERSHOOT = [(0, 150), (5, 240), (12, 330), (16, 335), (24, 270), (40, 220)]
# Closed by hand while the young fire is still climbing, which then sags
EARLY_CLOSE = [(0, 22), (7.5, 150), (14, 235), (17, 240), (30, 200)]

CASES = [
    ("guard holding a closed vent, then burn-out: coals", OCT24, 85, (), "coals", 0),
    ("closed by hand after the peak: coals", OCT24, 85, [(60, 0.75), (62, 1.0)], "coals", 0),
    ("guard closing hard, fire sags soon after: back-off", OVERSHOOT, 40, (), None, 1),
    ("closed by hand before the peak, fire sags: back-off", EARLY_CLOSE, 30, [(14, 0.6)], None, 1),
]


def main():
    failed = 0
    for name, points, minutes, manual, phase, rescues in CASES:
        ctl, phases = run(points, minutes, manual)
        ok = ctl.rescues >= rescues if rescues else ctl.rescues == 0
        ok = ok and (phase is None or ctl.phase == phase)
        failed += not ok
        print(f"{'ok  ' if ok else 'FAIL'} {name}: rescues={ctl.rescues} "
              + " ".join(f"{m}:{p}" for m, p in phases))
    print(f"{len(CASES) - failed}/{len(CASES)} checks passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
