"""Drive Hearthmate from a desktop: simulate the firmware's vent control, watch the live device.

Run with the marimo project's environment, from the repo root:

    uv run --project marimo python .claude/skills/run-hearthmate/driver.py <command> [options]

Commands:
    sim       run the firmware's VentCloser + TemperatureTargetFunction against a fitted stove
              model, with fake hardware; print the controller log, optionally plot to a PNG
    replay    replay recorded burns through the stall / coal-stage classifier
    status    one-shot snapshot of the live device's MQTT state topics (read-only)
    watch     stream the live device's controller events for a while (read-only)
    burnlog   tail the current burn's device log written by the StoveLink service
    check     syntax-check the firmware and run the StoveLink service tests

Settings come from the repo's .env (template: .env.example); the environment takes precedence.
"""

import argparse
import importlib
import json
import os
import py_compile
import shutil
import subprocess
import sys
import time
import types
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SIMULATION = REPO / "simulation"
# Firmware modules run on CPython, but the repo's logging.py shadows the stdlib logging (it needs
# adafruit_logging), so the repo goes at the END of sys.path.
sys.path.insert(0, str(SIMULATION))
sys.path.append(str(REPO))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO / ".env")
TOPIC = os.environ.get("HEARTHMATE_TOPIC", "burnie")
MQTT_HOST = os.environ.get("HEARTHMATE_MQTT_HOST")
BURNIE_DATA = Path(os.environ.get("BURNIE_DATA", "/Volumes/Burnie"))
# The host running the StoveLink service and its data directory, for files it still has open
STOVELINK_HOST = os.environ.get("STOVELINK_HOST")
STOVELINK_DATA = os.environ.get("STOVELINK_DATA", "/mnt/burnie")

FIRMWARE = [
    "code.py", "temperature_target.py", "vent_closer.py", "state_machine.py", "airvent.py",
    "hardware.py", "thermal_camera.py", "homeassistant.py", "connections.py", "vent_mover.py",
    "stovelink.py", "measurement_buffer.py", "hw_test.py", "burn_state.py",
]


# --- sim -------------------------------------------------------------------------------------

STEP_UNITS = 4096 / 200  # encoder units per motor step


class FakeHardware:
    """Stepper + AS5600 stand-in: steps move an unwrapped encoder angle."""

    def __init__(self, vent):
        self.vent = vent
        self.angle = float(vent.open_position)
        self.motor = types.SimpleNamespace(release=lambda: None)

    def read_raw_angle(self):
        return int(self.angle) % 4096

    def close_vent(self, steps, delay=0.05):
        self._move(+steps)

    def open_vent(self, steps, delay=0.05):
        self._move(-steps)

    def _move(self, steps):
        # quarter-revolution chunks so the Vent's zero-crossing tracking sees every crossing
        while steps:
            chunk = max(-40, min(40, steps))
            steps -= chunk
            self.angle += chunk * STEP_UNITS
            self.vent.update_from_hardware(self.read_raw_angle())

    def __getattr__(self, name):  # pixels, LEDs
        return lambda *a, **k: None


def cmd_sim(args):
    import logging

    logging.basicConfig(level=logging.WARNING if args.quiet else logging.INFO, format="%(message)s")
    from airvent import Vent
    from state_machine import StateMachine
    from temperature_target import TemperatureTargetFunction
    from vent_closer import VentCloser

    plant = importlib.import_module(f"plant_v{args.model}")
    fit = json.loads((SIMULATION / f"fit_all_v{args.model}.json").read_text())
    burn = fit["burns"][args.burn]

    clock = types.SimpleNamespace(t=0.0)
    func = TemperatureTargetFunction(time_func=lambda: clock.t, time_range=args.close_time)
    vent = Vent(open_position=300, closed_position=1200, num_zero_crossings=1)
    hardware = FakeHardware(vent)
    vent.update_from_hardware(hardware.read_raw_angle())
    machine = StateMachine()
    machine.data.update(hardware=hardware, vent=vent, mqtt_client=None)
    machine.add_state(VentCloser(func, name="temp_control"))
    machine.set_state("temp_control")

    stove = plant.Stove(fit["global"], burn["m0"] * args.load, burn["B0"], burn["T0"])
    outage = tuple(float(x) * 60 for x in args.outage.split(":")) if args.outage else None
    print(f"sim: model v{args.model}, burn {args.burn} ({burn['name']}), load x{args.load}")
    rows, last = [], (None, None)
    while clock.t < args.hours * 3600:
        if not (outage and outage[0] <= clock.t < outage[1]):
            func.update_temperature(stove.T)
        machine.update()
        position = min(max(vent.get_position(), 0.0), 1.0)
        if args.kindling is not None and clock.t == args.kindling * 60:
            stove.m += 0.25
            stove.B += 0.4
            print(f"  {clock.t / 60:6.1f} min  kindling added")
        substate = machine.states["temp_control"].machine.current_state
        # Phase changes, and the vent reaching or leaving closed; not every monitoring/closing step
        if (func.phase, substate == "closed") != last:
            print(f"  {clock.t / 60:6.1f} min  phase={func.phase:10s} substate={substate:10s} "
                  f"T={stove.T:5.1f}C vent={position:.3f} closed")
            last = (func.phase, substate == "closed")
        rows.append((clock.t / 60, stove.T, 1 - position, func.phase))
        stove.step(position)
        clock.t += 10
    peak = max(r[1] for r in rows)
    print(f"done: peak {peak:.0f}C, final phase {func.phase}, vent {1 - rows[-1][2]:.3f} closed")
    if args.png:
        plot(rows, args, Path(args.png))


def plot(rows, args, path):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    t = [r[0] for r in rows]
    fig, ax = plt.subplots(figsize=(10, 4.5))
    ax.plot(t, [r[1] for r in rows], color="tab:red", label="glass max temp")
    ax.axhline(300, color="gray", lw=0.6, ls="--")
    ax.set_xlabel("minutes since reload")
    ax.set_ylabel("°C")
    vent_ax = ax.twinx()
    vent_ax.plot(t, [r[2] for r in rows], color="tab:blue", ls=":", label="vent fraction open")
    vent_ax.set_ylim(0, 1.05)
    vent_ax.set_ylabel("fraction open")
    for i in range(1, len(rows)):
        if rows[i][3] != rows[i - 1][3]:
            ax.axvline(t[i], color="k", lw=0.4)
            ax.text(t[i], ax.get_ylim()[1], " " + rows[i][3], fontsize=7, va="top", rotation=90)
    ax.set_title(f"temp_control sim: model v{args.model}, burn {args.burn}")
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=80)
    print(f"plot: {path}")


# --- replay / check --------------------------------------------------------------------------

def cmd_replay(args):
    cmd = [sys.executable, "replay_stalls.py"] + (["fair"] if args.fair else [])
    sys.exit(subprocess.call(cmd, cwd=SIMULATION))


def cmd_check(args):
    failed = 0
    for name in FIRMWARE:
        try:
            py_compile.compile(str(REPO / name), doraise=True)
        except py_compile.PyCompileError as e:
            print(e.msg)
            failed += 1
    print(f"firmware syntax: {len(FIRMWARE) - failed}/{len(FIRMWARE)} files ok")
    uv = shutil.which("uv")
    result = subprocess.run([uv, "run", "python", "test_stovelink_service.py"],
                            cwd=REPO / "service", capture_output=True, text=True)
    for line in result.stdout.splitlines():
        if "All tests" in line or "✗" in line:
            print(f"service tests: {line.strip()}")
    # Soft reboots mid-burn: save, rebuild, restore, continue (~45 s)
    restart = subprocess.run([sys.executable, "restart.py"], cwd=SIMULATION,
                             capture_output=True, text=True)
    for line in restart.stdout.splitlines():
        if "FAIL" in line or "checks passed" in line:
            print(f"restart: {line.strip()}")
    # Trim mode's back-off vs coal stage decisions on scripted traces
    trim = subprocess.run([sys.executable, "trim_cases.py"], cwd=SIMULATION,
                          capture_output=True, text=True)
    for line in trim.stdout.splitlines():
        if "FAIL" in line or "checks passed" in line:
            print(f"trim cases: {line.strip()}")
    sys.exit(1 if failed or result.returncode or restart.returncode or trim.returncode else 0)


# --- live device (read-only MQTT) --------------------------------------------------------------

def mosquitto_sub(*args):
    exe = shutil.which("mosquitto_sub")
    if exe is None:
        sys.exit("mosquitto_sub not found on PATH")
    # Broker credentials come from ~/.config/mosquitto_sub (-u/-P lines), never from here
    return [exe] + (["-h", MQTT_HOST] if MQTT_HOST else []) + list(args)


STATE_TOPICS = [
    "status", "state", "air_vent/state", "thermal/max/state", "tmp36/state", "camera_ok/state",
    "temp_control/phase/state", "temp_control/fire_stalled/state",
    "temp_control/predicted/state", "temp_control/target/state", "service/state",
]


def cmd_status(args):
    cmd = mosquitto_sub("-W", str(args.seconds), "-F", "%t\t%p",
                        *sum((["-t", f"{TOPIC}/{t}"] for t in STATE_TOPICS), []))
    out = subprocess.run(cmd, capture_output=True, text=True).stdout
    latest = {}
    for line in out.splitlines():
        if "\t" in line:
            topic, payload = line.split("\t", 1)
            latest[topic[len(TOPIC) + 1:]] = payload
    if not latest:
        sys.exit(f"no messages on {TOPIC}/... in {args.seconds}s (device offline or broker auth?)")
    for t in STATE_TOPICS:
        if t in latest:
            print(f"{t:32s} {latest[t]}")
    print(f"({len(latest)}/{len(STATE_TOPICS)} topics seen in {args.seconds}s; "
          "unretained topics only appear when they change)")


EVENT_WORDS = ("temp_control ", "Closed", "override", "Override", "Detected vent", "recover",
               "Traceback", " ERROR ", " WARNING ")


def cmd_watch(args):
    cmd = mosquitto_sub("-W", str(int(args.minutes * 60)), "-F", "%I\t%t\t%p",
                        "-t", f"{TOPIC}/log", "-t", f"{TOPIC}/temp_control/phase/state",
                        "-t", f"{TOPIC}/temp_control/fire_stalled/state", "-t", f"{TOPIC}/status",
                        "-t", f"{TOPIC}/camera_ok/state")
    print(f"watching {TOPIC}/... for {args.minutes} min", flush=True)
    with subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True) as proc:
        for line in proc.stdout:
            parts = line.rstrip("\n").split("\t", 2)
            if len(parts) < 3:
                continue
            when, topic, payload = parts
            if topic.endswith("/log") and not any(w in payload for w in EVENT_WORDS):
                continue
            print(f"{when[11:19]} {topic[len(TOPIC) + 1:]}: {payload}", flush=True)


def cmd_burnlog(args):
    if args.file:
        # Archived burns are closed files: the share reads them fine
        text = (BURNIE_DATA / args.file).read_text(errors="replace")
    elif STOVELINK_HOST:
        # The share serves stale cached contents of the log the service is still appending to, so
        # read it on the service's host
        result = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", STOVELINK_HOST,
             f"tail -n {args.lines * 3} {STOVELINK_DATA}/current_cycle.log"],
            capture_output=True, text=True)
        if result.returncode:
            sys.exit(f"ssh {STOVELINK_HOST} failed: {result.stderr.strip()}")
        text = result.stdout
    else:
        sys.exit("set STOVELINK_HOST in .env to read the current burn's log, or pass --file")
    entries = []
    for line in text.splitlines():
        if line.startswith("\t") and entries:
            entries[-1] += "\n" + " " * 26 + line.strip()  # continuation, e.g. a stack trace
        elif line.strip():
            received, seq, comb, message = (line.split("\t", 3) + ["", "", ""])[:4]
            entries.append(f"{received[11:19]} seq={seq:>5} t={comb:>5}s {message}")
    print("\n".join(entries[-args.lines:]))


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("sim", help="simulate the firmware's temp_control against a stove model")
    p.add_argument("--model", type=int, choices=(2, 3), default=3)
    p.add_argument("--burn", type=int, default=40, help="fitted burn index (0-89)")
    p.add_argument("--load", type=float, default=1.0, help="fuel load multiplier")
    p.add_argument("--hours", type=float, default=4.0)
    p.add_argument("--close-time", type=float, default=2700, help="Closing Duration, s")
    p.add_argument("--outage", help="camera outage START:END in minutes, e.g. 3:15")
    p.add_argument("--kindling", type=float, help="add kindling at this minute")
    p.add_argument("--png", help="save a plot of temperature, vent and phases")
    p.add_argument("--quiet", action="store_true", help="hide the controller's log lines")
    p.set_defaults(func=cmd_sim)

    p = sub.add_parser("replay", help="replay recorded burns through the classifier")
    p.add_argument("--fair", action="store_true", help="count only controller-like closes")
    p.set_defaults(func=cmd_replay)

    p = sub.add_parser("check", help="firmware syntax check + StoveLink service tests")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("status", help="snapshot of the live device's state topics")
    p.add_argument("--seconds", type=int, default=15)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("watch", help="stream live controller events")
    p.add_argument("--minutes", type=float, default=5)
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("burnlog", help="tail the current burn's device log")
    p.add_argument("--lines", type=int, default=20)
    p.add_argument("--file", help="an archived log under BURNIE_DATA, e.g. 202610/17/20261017-08.log")
    p.set_defaults(func=cmd_burnlog)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
