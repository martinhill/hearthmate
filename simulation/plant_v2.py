"""Grey-box wood stove model: glass-door max temperature vs vent position.

States:
  m  remaining fuel (normalized; ~1 = typical load)
  B  fire intensity / burn rate (1/h); lags its air- and fuel-limited target with tau_b
  Tb slow stove-body/glass bulk temperature, heated by the fire (tau_s)
Measured glass temperature T = Tb + K_f * B  (fast radiative component through the flames)

u is vent closedness (0 open, 1 closed), as in airvent.Vent.
"""
import numpy as np

T_AMB = 25.0
NAMES = ["k_b", "p", "gamma", "a0", "tau_b", "K_s", "tau_s", "K_f"]
DEFAULT = dict(k_b=0.3, p=0.7, gamma=1.0, a0=0.3, tau_b=300.0, K_s=200.0, tau_s=1800.0, K_f=100.0)
LO = dict(k_b=0.01, p=0.2, gamma=0.1, a0=0.02, tau_b=30, K_s=10, tau_s=300, K_f=0.0)
HI = dict(k_b=5.0, p=2.0, gamma=4.0, a0=0.95, tau_b=1800, K_s=2000, tau_s=7200, K_f=1000)


def air(u, a0):
    return a0 + (1 - a0) * (1 - u)


class Stove:
    def __init__(self, params, m0=1.0, B0=0.3, T0=150.0, dt=10.0):
        self.p = dict(params)
        self.m, self.B, self.dt = m0, B0, dt
        self.Tb = T0 - self.p["K_f"] * B0

    @property
    def T(self):
        return self.Tb + self.p["K_f"] * self.B

    def step(self, u):
        P, dt = self.p, self.dt
        a = air(min(max(u, 0.0), 1.0), P["a0"])
        B_star = P["k_b"] * max(self.m, 0.0) ** P["p"] * a ** P["gamma"]
        self.B += (B_star - self.B) * dt / P["tau_b"]
        self.m = max(0.0, self.m - self.B * dt / 3600)
        Tb_eq = T_AMB + P["K_s"] * self.B
        self.Tb += (Tb_eq - self.Tb) * dt / P["tau_s"]
        return self.T


def simulate(params, u_seq, m0, B0, T0, dt=10.0):
    s = Stove(params, m0, B0, T0, dt)
    return np.array([s.step(u) for u in u_seq])
