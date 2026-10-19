"""Grey-box wood stove model: glass-door max temperature vs vent position.

States:
  m  remaining fuel (normalized; ~1 = typical load)
  B  burn rate / fire intensity (1/h), lags its target with tau_b
  Tb slow stove-body/glass bulk temperature, heated by the fire (tau_s)
Measured glass temperature T = Tb + K_f * B  (fast radiative component from the flames)

The burn-rate target is a soft minimum of a fuel-limited rate (pyrolysis, driven by remaining
fuel and by how hot the stove is) and an air-limited rate (proportional to vent airflow):
with plenty of air the vent hardly matters, with restricted air it dominates, and starving a
cool fire lets it stall (positive feedback through Tb).

u is vent closedness (0 open, 1 closed), as in airvent.Vent.
"""
import numpy as np

T_AMB = 25.0
SOFTMIN_N = 4.0
NAMES = ["k_f", "p", "q", "k_a", "a0", "tau_b", "K_s", "tau_s", "K_f"]
DEFAULT = dict(k_f=0.8, p=0.8, q=1.0, k_a=1.0, a0=0.2, tau_b=180.0, K_s=250.0, tau_s=2400.0, K_f=80.0)
LO = dict(k_f=0.01, p=0.2, q=0.0, k_a=0.05, a0=0.02, tau_b=30, K_s=10, tau_s=300, K_f=0.0)
HI = dict(k_f=10.0, p=2.0, q=3.0, k_a=20.0, a0=0.8, tau_b=1200, K_s=2000, tau_s=7200, K_f=500)


def air(u, a0):
    return a0 + (1 - a0) * (1 - u)


class Stove:
    def __init__(self, params, m0=1.0, B0=0.3, T0=150.0, dt=10.0):
        self.p = dict(params)
        self.m, self.B, self.dt = m0, B0, dt
        self.Tb = max(T_AMB, T0 - self.p["K_f"] * B0)

    @property
    def T(self):
        return self.Tb + self.p["K_f"] * self.B

    def step(self, u):
        P, dt = self.p, self.dt
        a = air(min(max(u, 0.0), 1.0), P["a0"])
        b_fuel = P["k_f"] * max(self.m, 1e-6) ** P["p"] * (max(self.Tb, T_AMB) / 300.0) ** P["q"]
        b_air = P["k_a"] * a
        B_star = (b_fuel ** -SOFTMIN_N + b_air ** -SOFTMIN_N) ** (-1 / SOFTMIN_N)
        self.B += (B_star - self.B) * dt / P["tau_b"]
        self.m = max(0.0, self.m - self.B * dt / 3600)
        self.Tb += (T_AMB + P["K_s"] * self.B - self.Tb) * dt / P["tau_s"]
        return self.T


def simulate(params, u_seq, m0, B0, T0, dt=10.0):
    s = Stove(params, m0, B0, T0, dt)
    return np.array([s.step(u) for u in u_seq])
