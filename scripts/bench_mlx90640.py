"""Compare the two MLX90640 temperature calculations: the per-pixel loop of the forked driver
(adafruit_mlx90640.py) and the ulab one (numpy_mlx90640.py).

    uv run --with numpy python scripts/bench_mlx90640.py check [CAPTURE]
    uv run --with numpy python scripts/bench_mlx90640.py device [--out FILE] [--repeats N]
    uv run --with numpy python scripts/bench_mlx90640.py bus [--reads N]

check   runs both drivers unchanged on the desktop, against an emulated sensor, and fails if
        they disagree. ulab is stood in for by numpy, in float64 (must match the per-pixel loop to
        1e-6 C) and float32 (the device's precision; reported only). Without CAPTURE the sensor
        uses the calibration example of the MLX90640 datasheet (Table 12) with a made-up scene, in
        both reading modes, plus a bad frame and a second driver instance. Desktop timings say
        nothing about the device, so there are none.
device  sends the device the bench_camera command, which times both calculations on the same
        raw frames (mlx90640_bench.py), prints its results from the log, saves the raw capture it
        publishes (EEPROM and both subpages, in reference/ unless --out) and runs check on it.
        The main loop stops for the bench (about 5 s with 3 repeats), so run it while idle
        rather than mid-burn.

bus     sends the device the bench_i2c command, which checks the I2C bus at its clock
        (I2C_FREQUENCY in settings.toml): reads the camera EEPROM repeatedly, which must match
        the copy read at init word for word, times the frame RAM read and reads the encoder.

The device commands use mosquitto_pub/mosquitto_sub, with HEARTHMATE_TOPIC and
HEARTHMATE_MQTT_HOST from .env and the broker credentials in ~/.config/mosquitto_{pub,sub}.
"""

import argparse
import os
import shutil
import subprocess
import sys
import time
import types
from datetime import datetime
from pathlib import Path

import numpy

REPO = Path(__file__).resolve().parents[1]
# The repo's logging.py shadows the stdlib logging (it imports adafruit_logging), so the repo goes
# at the END of sys.path.
sys.path.append(str(REPO))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO / ".env")

CAPTURE_WORDS = 832 + 2 * 834  # EEPROM, then two raw subpage frames (mlx90640_bench.capture())
EXACT_TOLERANCE = 1e-6  # C, float64
FLOAT32_TOLERANCE = 0.05  # C, reported only

# --- stand-ins for the CircuitPython modules ---------------------------------------------------

ulab_numpy = types.ModuleType("ulab.numpy")
ulab_numpy.__dict__.update({k: getattr(numpy, k) for k in dir(numpy) if not k.startswith("__")})
ulab_numpy.float = numpy.float64
ulab = types.ModuleType("ulab")
ulab.numpy = ulab_numpy
sys.modules["ulab"] = ulab
sys.modules["ulab.numpy"] = ulab_numpy


class FakeSensor:
    """MLX90640 memory: EEPROM at 0x2400, RAM at 0x0400 from a list of raw subpage frames (834
    words: RAM, control register, subpage). Clearing data ready in the status register moves on
    to the next frame."""

    def __init__(self, eeprom, frames):
        self.eeprom = list(eeprom)
        self.frames = [list(frame) for frame in frames]
        self.index = 0

    @property
    def frame(self):
        return self.frames[self.index % len(self.frames)]

    def read(self, addr):
        if 0x2400 <= addr < 0x2400 + 832:
            return self.eeprom[addr - 0x2400]
        if 0x0400 <= addr < 0x0400 + 832:
            return self.frame[addr - 0x0400]
        if addr == 0x8000:
            return 0x0008 | self.frame[833]  # new data available, subpage
        if addr == 0x800D:
            return self.frame[832]
        return 0

    def write(self, addr, value):
        if addr == 0x8000 and not value & 0x0008:
            self.index += 1


class FakeI2CDevice:
    """adafruit_bus_device.i2c_device.I2CDevice on a FakeSensor (FakeI2CDevice.sensor)"""

    sensor = None

    def __init__(self, i2c, address):
        self.sensor = FakeI2CDevice.sensor

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def write(self, buf):
        self.sensor.write(buf[0] << 8 | buf[1], buf[2] << 8 | buf[3])

    def write_then_readinto(self, out, inbuf, in_end=None):
        addr = out[0] << 8 | out[1]
        for i in range((len(inbuf) if in_end is None else in_end) // 2):
            word = self.sensor.read(addr + i)
            inbuf[2 * i] = word >> 8
            inbuf[2 * i + 1] = word & 0xFF


i2c_device = types.ModuleType("adafruit_bus_device.i2c_device")
i2c_device.I2CDevice = FakeI2CDevice
bus_device = types.ModuleType("adafruit_bus_device")
bus_device.i2c_device = i2c_device
sys.modules["adafruit_bus_device"] = bus_device
sys.modules["adafruit_bus_device.i2c_device"] = i2c_device

import adafruit_mlx90640  # noqa: E402
import numpy_mlx90640  # noqa: E402

# --- the datasheet sensor ----------------------------------------------------------------------

# EEPROM 0x2410...0x243F of the datasheet's calculation example (Table 12)
DATASHEET_CALIBRATION = [
    0x4210, 0xFFBB, 0x0202, 0xF202, 0xF2F2, 0xE2E2, 0xD1E1, 0xB1D1,
    0xF10F, 0xF00F, 0xE0EF, 0xE0EF, 0xE1E1, 0xF3F2, 0xF404, 0xE504,
    0x79A6, 0x2F44, 0xFFDD, 0x2210, 0x3333, 0x2233, 0xEF01, 0x9ACC,
    0xEEDC, 0x10FF, 0x2221, 0x3333, 0x2333, 0x0112, 0xEEFF, 0xBBDD,
    0x18EF, 0x2FF1, 0x5952, 0x9D68, 0x5454, 0x0994, 0x6956, 0x5354,
    0x2363, 0xE446, 0xFBB5, 0x044B, 0xF020, 0x9797, 0x9797, 0x2889,
]
OUTLIER_PIXEL = 100
BROKEN_PIXEL = 400


def datasheet_eeprom(seed=1):
    """The datasheet calibration with made-up pixel words (offset, alpha and kta within a few
    steps of the averages), one outlier and one broken pixel"""
    rng = numpy.random.default_rng(seed)
    eeprom = [0] * 832
    eeprom[16:64] = DATASHEET_CALIBRATION
    for p in range(768):
        offset = int(rng.integers(-8, 9)) & 0x3F
        alpha = int(rng.integers(1, 12)) & 0x3F
        kta = int(rng.integers(-2, 3)) & 0x7
        eeprom[64 + p] = offset << 10 | alpha << 4 | kta << 1
    eeprom[64 + OUTLIER_PIXEL] |= 1
    eeprom[64 + BROKEN_PIXEL] = 0
    return eeprom


def datasheet_frames(chess=True, seed=2):
    """Two raw subpage frames of a scene from below 0 C to about 400 C, with the datasheet's
    auxiliary data (Table 11). The datasheet's pixel reads 609 at 80 C."""
    rng = numpy.random.default_rng(seed)
    rows, cols = numpy.mgrid[0:24, 0:32]
    scene = 150 + 40 * rng.standard_normal((24, 32))  # around ambient
    scene[6:18, 8:24] = numpy.linspace(600, 20000, 12 * 16).reshape(12, 16)  # the glass door
    scene[20:, :4] = -250  # something cold
    frames = []
    for subpage in (0, 1):
        raw = numpy.round(scene + 5 * rng.standard_normal((24, 32))).astype(numpy.int64)
        frame = [int(v) & 0xFFFF for v in raw.ravel()] + [0] * 66
        frame[768] = 0x4BF2  # Vbe
        frame[776] = 0xFFCA  # CP subpage 0
        frame[778] = 0x1881  # gain
        frame[800] = 0x06AF  # PTAT
        frame[808] = 0xFFC8  # CP subpage 1
        frame[810] = 0xCCC5  # VDD
        frame[832] = 0x1901 if chess else 0x0901  # control register
        frame[833] = subpage
        frames.append(frame)
    return frames


def read_capture(path):
    words = numpy.frombuffer(Path(path).read_bytes(), dtype="<u2")
    if len(words) != CAPTURE_WORDS:
        sys.exit(f"{path}: {len(words) * 2} bytes, expected {CAPTURE_WORDS * 2}")
    words = [int(w) for w in words]
    return words[:832], [words[832:1666], words[1666:]]


# --- check -------------------------------------------------------------------------------------


def make_drivers(eeprom, frames, dtype):
    ulab_numpy.float = dtype
    FakeI2CDevice.sensor = FakeSensor(eeprom, frames)
    scalar = adafruit_mlx90640.MLX90640(None)
    FakeI2CDevice.sensor = FakeSensor(eeprom, frames)
    vector = numpy_mlx90640.MLX90640(None)
    return scalar, vector


def compare(name, eeprom, frames, dtype, tolerance):
    """Both getFrame()s and both _CalculateTo()s on each frame; returns the largest difference"""
    scalar, vector = make_drivers(eeprom, frames, dtype)
    scalar_result = [0.0] * 768
    vector_result = numpy.zeros(768, dtype=dtype)
    scalar.getFrame(scalar_result)
    vector.getFrame(vector_result)
    worst = abs(numpy.array(scalar_result) - vector_result).max()
    for frame in frames:
        tr = scalar._GetTa(frame) - adafruit_mlx90640.OPENAIR_TA_SHIFT
        scalar._CalculateTo(frame, 0.95, tr, scalar_result)
        vector._CalculateTo(numpy.array(frame, dtype=numpy.uint16), 0.95, tr, vector_result)
        worst = max(worst, abs(numpy.array(scalar_result) - vector_result).max())
    good = numpy.array(scalar_result) > -273
    hottest = int(numpy.argmax(numpy.where(good, scalar_result, -numpy.inf)))
    print(
        f"{name:34} {numpy.dtype(dtype).name:7}  max diff {worst:.2e} C   "
        f"range {min(numpy.array(scalar_result)[good]):6.1f} .. {scalar_result[hottest]:6.1f} C   "
        f"deviating pixels {scalar.brokenPixels} {scalar.outlierPixels}"
    )
    if worst > tolerance:
        print(f"  FAIL: differs by more than {tolerance} C")
        return False
    return True


def check_bad_frame(eeprom, frames):
    """A pixel far below the sensor's range: math.sqrt() raises ValueError, so must the ulab one"""
    frames = [list(frame) for frame in frames]
    frames[0][32 * 3 + 4 + (frames[0][833] + 3) % 2] = (-32000) & 0xFFFF
    scalar, vector = make_drivers(eeprom, frames, numpy.float64)
    outcomes = []
    for driver, result in ((scalar, [0.0] * 768), (vector, numpy.zeros(768))):
        try:
            driver.getFrame(result)
            outcomes.append("no error")
        except ValueError as e:
            outcomes.append(f"ValueError: {e}")
    ok = outcomes[0] == outcomes[1] != "no error"
    print(f"{'bad frame':34} per-pixel {outcomes[0]}, ulab {outcomes[1]}{'' if ok else '  FAIL'}")
    return ok


def check_second_instance(eeprom, frames):
    """Camera recovery creates a new driver instance; deviating pixels must not accumulate"""
    try:
        make_drivers(eeprom, frames, numpy.float64)
        make_drivers(eeprom, frames, numpy.float64)
        print(f"{'second driver instance':34} ok")
        return True
    except RuntimeError as e:
        print(f"{'second driver instance':34} FAIL: {e}")
        return False


def cmd_check(args):
    ok = True
    if args.capture:
        eeprom, frames = read_capture(args.capture)
        name = Path(args.capture).name
        ok &= compare(name, eeprom, frames, numpy.float64, EXACT_TOLERANCE)
        compare(name, eeprom, frames, numpy.float32, FLOAT32_TOLERANCE)
    else:
        eeprom = datasheet_eeprom()
        for chess in (True, False):
            frames = datasheet_frames(chess)
            name = f"datasheet, {'chess' if chess else 'interleaved'} mode"
            ok &= compare(name, eeprom, frames, numpy.float64, EXACT_TOLERANCE)
            compare(name, eeprom, frames, numpy.float32, FLOAT32_TOLERANCE)
        frames = datasheet_frames()
        ok &= check_bad_frame(eeprom, frames)
        ok &= check_second_instance(eeprom, frames)
    print("ok" if ok else "FAILED")
    return 0 if ok else 1


# --- device ------------------------------------------------------------------------------------


def mosquitto(tool, *args):
    exe = shutil.which(tool)
    if exe is None:
        sys.exit(f"{tool} not found on PATH")
    host = os.getenv("HEARTHMATE_MQTT_HOST")
    # Broker credentials come from ~/.config/mosquitto_{pub,sub} (-u/-P lines), never from here
    return [exe] + (["-h", host] if host else []) + list(args)


def run_command(topic, command):
    """Send the device a bench command and print its log lines until it's done. True if it
    finished."""
    name = command.split()[0]
    log = subprocess.Popen(
        mosquitto("mosquitto_sub", "-t", f"{topic}/log", "-W", "180"),
        stdout=subprocess.PIPE, text=True,
    )
    time.sleep(1)
    subprocess.run(
        mosquitto("mosquitto_pub", "-t", f"{topic}/command", "-m", command), check=True
    )
    print(f"{name} sent to {topic}; the device's main loop stops until it's done\n")
    done = False
    for line in log.stdout:
        if name in line:
            print(line.rstrip())
        if f"{name}: done" in line or f"{name} failed" in line:
            done = "done" in line
            break
        if f"Unknown command: {name}" in line or f"{name}: no MLX90640" in line:
            break
        if f"{name}: needs" in line:
            break
    log.terminate()
    return done


def cmd_device(args):
    topic = os.getenv("HEARTHMATE_TOPIC") or sys.exit("Set HEARTHMATE_TOPIC in .env")
    raw = subprocess.Popen(
        mosquitto("mosquitto_sub", "-t", f"{topic}/bench/raw", "-C", "1", "-N", "-W", "180"),
        stdout=subprocess.PIPE,
    )
    if not run_command(topic, f"bench_camera {args.repeats}"):
        raw.terminate()
        return 1
    try:
        capture, _ = raw.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        raw.terminate()
        capture = b""
    if len(capture) != CAPTURE_WORDS * 2:
        print(f"\nNo raw capture on {topic}/bench/raw ({len(capture)} bytes)")
        return 1
    out = args.out or REPO / "reference" / (
        datetime.now().strftime("mlx90640-capture-%Y%m%d-%H%M%S.bin")
    )
    Path(out).write_bytes(capture)
    print(f"\nRaw capture saved to {out}\n")
    args.capture = out
    return cmd_check(args)


def cmd_bus(args):
    topic = os.getenv("HEARTHMATE_TOPIC") or sys.exit("Set HEARTHMATE_TOPIC in .env")
    return 0 if run_command(topic, f"bench_i2c {args.reads}") else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="compare both drivers on the desktop")
    check.add_argument("capture", nargs="?", help="raw capture from the device")
    device = sub.add_parser("device", help="run bench_camera on the device, then check")
    device.add_argument("--out", help="where to save the raw capture")
    device.add_argument("--repeats", type=int, default=3, help="timed calculations per subpage")
    bus = sub.add_parser("bus", help="run bench_i2c on the device (I2C errors and speed)")
    bus.add_argument("--reads", type=int, default=50, help="camera EEPROM reads")
    args = parser.parse_args()
    sys.exit({"check": cmd_check, "device": cmd_device, "bus": cmd_bus}[args.command](args))


if __name__ == "__main__":
    main()
