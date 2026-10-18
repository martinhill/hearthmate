"""Grey-box wood stove model v4: v3 plus the effect of excess air.

v3 has the vent act only through the burn rate, so closing can never raise the temperature. On
the stove it often does (burn of 18 Oct 2026: closing 58% -> 43% open while the rise had slowed
to +2 C/min brought it back to +8 to +9 C/min within 2 minutes). Air beyond what the fire burns
dilutes the flame gases and carries heat up the chimney, so with a fuel-limited fire, less air
means hotter glass, until the fire becomes air-limited and less air means less fire.

States as in v3:
  m  remaining fuel (normalized; ~1 = typical load)
  B  burn rate / fire intensity (1/h), lags its target with tau_b
  Tb slow stove-body/glass bulk temperature, heated by the fire (tau_s)
Excess air ratio lam = air-limited burn rate / burn rate (>= ~1; 1 = all the air is used).
Measured glass temperature T = Tb + K_f * B * (1 + d) / (1 + d * lam): the flame/gas term is
diluted by excess air (d >= 0; d = 0 is v3). The body keeps the share 1 / (1 + e * (lam - 1))
of the heat (e >= 0; e = 0 is v3); the rest leaves up the chimney.

u is vent closedness (0 open, 1 closed), as in airvent.Vent.
"""
import numpy as np

T_AMB = 25.0
SOFTMIN_N = 4.0
NAMES = ["k_f", "p", "q", "k_a", "a0", "tau_b", "K_s", "tau_s", "K_f", "d", "e"]
DEFAULT = dict(k_f=0.8, p=0.8, q=1.0, k_a=1.0, a0=0.2, tau_b=180.0, K_s=250.0, tau_s=2400.0,
               K_f=80.0, d=0.5, e=0.2)
# K_f at least 20: without a fast flame term the fit drops it (K_f 0.01) and follows only the
# slow body temperature, which can't show the 1-2 minute response to a vent step
LO = dict(k_f=0.01, p=0.2, q=0.0, k_a=0.05, a0=0.02, tau_b=30, K_s=10, tau_s=300, K_f=20.0,
          d=0.0, e=0.0)
HI = dict(k_f=10.0, p=2.0, q=3.0, k_a=20.0, a0=0.8, tau_b=1200, K_s=2000, tau_s=7200, K_f=500,
          d=5.0, e=5.0)


def air(u, a0):
    return a0 + (1 - a0) * (1 - u)


class Stove:
    def __init__(self, params, m0=1.0, B0=0.3, T0=150.0, dt=10.0, u0=0.0):
        self.p = dict(params)
        self.m, self.B, self.dt = m0, B0, dt
        self.lam = self._lam(u0)
        self.Tb = max(T_AMB, T0 - self.p["K_f"] * B0 * self._dilution())

    def _lam(self, u):
        b_air = self.p["k_a"] * air(min(max(u, 0.0), 1.0), self.p["a0"])
        return max(1.0, b_air / max(self.B, 1e-3))

    def _dilution(self):
        d = self.p["d"]
        return (1 + d) / (1 + d * self.lam)

    @property
    def T(self):
        return self.Tb + self.p["K_f"] * self.B * self._dilution()

    def step(self, u):
        P, dt = self.p, self.dt
        a = air(min(max(u, 0.0), 1.0), P["a0"])
        b_fuel = P["k_f"] * max(self.m, 1e-6) ** P["p"] * (max(self.Tb, T_AMB) / 300.0) ** P["q"]
        b_air = P["k_a"] * a
        B_star = (b_fuel ** -SOFTMIN_N + b_air ** -SOFTMIN_N) ** (-1 / SOFTMIN_N)
        self.B += (B_star - self.B) * dt / P["tau_b"]
        self.m = max(0.0, self.m - self.B * dt / 3600)
        self.lam = max(1.0, b_air / max(self.B, 1e-3))
        kept = 1.0 / (1.0 + P["e"] * (self.lam - 1.0))
        self.Tb += (T_AMB + P["K_s"] * self.B * kept - self.Tb) * dt / P["tau_s"]
        return self.T


def simulate(params, u_seq, m0, B0, T0, dt=10.0):
    s = Stove(params, m0, B0, T0, dt, u0=u_seq[0] if len(u_seq) else 0.0)
    return np.array([s.step(u) for u in u_seq])
