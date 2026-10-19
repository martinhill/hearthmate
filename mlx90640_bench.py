"""
Time the MLX90640 temperature calculation of the per-pixel driver (adafruit_mlx90640) against the
ulab one (numpy_mlx90640) on the same raw frames, on the device: the bench_camera command.
scripts/bench_mlx90640.py sends the command, prints the results and checks the raw capture on the
desktop.
"""

import gc
import time

from ulab import numpy as np

import adafruit_mlx90640
import numpy_mlx90640

EMISSIVITY = 0.95  # as in getFrame()


def _ms(ns):
    return ns / 1e6


def _time_calculation(driver, frames, result, repeats, feed):
    """_CalculateTo() on each frame, repeats times. Returns the times (ns) and the bytes allocated
    by each call, or None on a ValueError (a bad frame)."""
    times = []
    allocated = []
    for _ in range(repeats):
        for frame in frames:
            tr = driver._GetTa(frame) - adafruit_mlx90640.OPENAIR_TA_SHIFT
            gc.collect()
            before = gc.mem_alloc()
            start = time.monotonic_ns()
            try:
                driver._CalculateTo(frame, EMISSIVITY, tr, result)
            except ValueError:
                return None
            times.append(time.monotonic_ns() - start)
            allocated.append(gc.mem_alloc() - before)
        feed()
    return times, allocated


def _time_ns(function, *args):
    start = time.monotonic_ns()
    function(*args)
    return time.monotonic_ns() - start


def run(i2c, scalar, log, repeats=3, feed=None):
    """
    Capture both subpages once with the camera's driver, then time each driver's calculation on
    them, the frame RAM read and a whole getFrame(), and compare the temperatures.

    Args:
        i2c: the camera's I2C bus
        scalar: the camera's adafruit_mlx90640.MLX90640
        log: logger for the results
        repeats: timed calculations per subpage and driver
        feed: called between the steps (the watchdog), or None

    Returns:
        bytes: the raw capture for scripts/bench_mlx90640.py check: the EEPROM, then the two
        subpage frames, as uint16 little endian
    """
    feed = feed or (lambda: None)
    start = time.monotonic_ns()

    # Both subpages, the way getFrame() reads them
    raws = []
    subpage = None
    for _ in range(2):
        raw = [0] * 834
        status = scalar._GetFrameData(raw, subpage)
        raws.append(raw)
        subpage = 0 if status else 1
    feed()

    gc.collect()
    free_before = gc.mem_free()
    init_ns = time.monotonic_ns()
    vector = numpy_mlx90640.MLX90640(i2c)
    init_ns = time.monotonic_ns() - init_ns
    prepare_ns = _time_ns(vector._PreparePixelArrays)
    gc.collect()
    vector_memory = free_before - gc.mem_free()
    feed()

    # The frame RAM read alone: struct.unpack() into a list, np.frombuffer() into an array
    read_list_ns = _time_ns(lambda: scalar._I2CReadWords(0x0400, [0] * 834, end=832))
    read_array_ns = _time_ns(lambda: vector._I2CReadWords(0x0400, vector._frame, end=832))

    scalar_result = [0.0] * 768
    vector_result = np.zeros(768, dtype=np.float)
    scalar_timing = _time_calculation(scalar, raws, scalar_result, repeats, feed)
    vector_frames = [np.array(raw, dtype=np.uint16) for raw in raws]
    vector_timing = _time_calculation(vector, vector_frames, vector_result, repeats, feed)

    mode = "chess" if raws[0][832] & 0x1000 else "interleaved"
    log.info(
        "bench_camera: Ta %.1fC, %s mode, broken pixels %s, outliers %s",
        scalar._GetTa(raws[0]),
        mode,
        scalar.brokenPixels,
        scalar.outlierPixels,
    )
    if scalar_timing is None or vector_timing is None:
        log.warning(
            "bench_camera: bad frame (ValueError) in the per-pixel %s, ulab %s calculation",
            "yes" if scalar_timing is None else "no",
            "yes" if vector_timing is None else "no",
        )
    else:
        scalar_times, scalar_allocated = scalar_timing
        vector_times, vector_allocated = vector_timing
        scalar_mean = sum(scalar_times) / len(scalar_times)
        vector_mean = sum(vector_times) / len(vector_times)
        log.info(
            "bench_camera: calculation per subpage: per-pixel %.0f ms (min %.0f), "
            "ulab %.1f ms (min %.1f), %.0fx faster",
            _ms(scalar_mean),
            _ms(min(scalar_times)),
            _ms(vector_mean),
            _ms(min(vector_times)),
            scalar_mean / vector_mean,
        )
        log.info(
            "bench_camera: allocated per subpage: per-pixel %d bytes, ulab %d bytes",
            max(scalar_allocated),
            max(vector_allocated),
        )
        worst = 0.0
        worst_pixel = 0
        for pixel in range(768):
            difference = abs(scalar_result[pixel] - vector_result[pixel])
            if difference > worst:
                worst = difference
                worst_pixel = pixel
        log.info(
            "bench_camera: results differ by up to %.4fC (pixel %d: %.2fC vs %.2fC); "
            "max pixel %.2fC vs %.2fC",
            worst,
            worst_pixel,
            scalar_result[worst_pixel],
            vector_result[worst_pixel],
            max(scalar_result),
            np.max(vector_result),
        )
    log.info(
        "bench_camera: frame RAM read: list %.1f ms, ulab %.1f ms; "
        "ulab driver init %.0f ms (calibration arrays %.0f ms), %d bytes",
        _ms(read_list_ns),
        _ms(read_array_ns),
        _ms(init_ns),
        _ms(prepare_ns),
        vector_memory,
    )
    feed()

    # Whole frames, including waiting for the sensor's subpages
    try:
        scalar_frame_ns = _time_ns(scalar.getFrame, scalar_result)
        feed()
        vector_frame_ns = _time_ns(vector.getFrame, vector_result)
        log.info(
            "bench_camera: getFrame(): per-pixel %.0f ms, ulab %.0f ms",
            _ms(scalar_frame_ns),
            _ms(vector_frame_ns),
        )
    except (ValueError, TimeoutError) as e:
        log.warning("bench_camera: getFrame() failed: %s", e)
    feed()

    # bytes: minimqtt doesn't publish what ulab's tobytes() returns
    capture = b"" + np.array(adafruit_mlx90640.eeData, dtype=np.uint16).tobytes()
    for raw in raws:
        capture += np.array(raw, dtype=np.uint16).tobytes()
    del vector
    gc.collect()
    log.info("bench_camera: done in %.1f s", _ms(time.monotonic_ns() - start) / 1000)
    return capture


def check_bus(frequency, mlx, hardware, log, reads=50, feed=None):
    """
    Check the I2C bus at its current clock (the bench_i2c command): read the camera's EEPROM
    repeatedly and compare each word with the copy read at init, time the frame RAM read, and
    read the encoder angle repeatedly (the vent doesn't move meanwhile).

    Args:
        frequency: the I2C clock in Hz, for the log (busio.I2C doesn't report it)
        mlx: the camera's adafruit_mlx90640.MLX90640
        hardware: the Hardware (encoder)
        log: logger for the results
        reads: EEPROM reads; the encoder is read 10 times as often
        feed: called between the steps (the watchdog), or None
    """
    feed = feed or (lambda: None)
    start = time.monotonic_ns()
    expected = np.array(adafruit_mlx90640.eeData, dtype=np.uint16)
    expected.byteswap(inplace=True)  # big endian, as on the bus
    expected = bytes(expected.tobytes())
    addr = bytearray(2)
    inbuf = bytearray(2 * 832)

    def read_words(address, words):
        addr[0] = address >> 8
        addr[1] = address & 0xFF
        with mlx.i2c_device as device:
            device.write_then_readinto(addr, inbuf, in_end=words * 2)

    # The EEPROM: every word must match
    errors = 0
    bad_reads = 0
    bad_words = 0
    eeprom_ns = []
    for _ in range(reads):
        t = time.monotonic_ns()
        try:
            read_words(0x2400, 832)
        except OSError:
            errors += 1
            continue
        eeprom_ns.append(time.monotonic_ns() - t)
        if inbuf != expected:
            bad_reads += 1
            for i in range(0, 2 * 832, 2):
                if inbuf[i] != expected[i] or inbuf[i + 1] != expected[i + 1]:
                    bad_words += 1
    feed()

    # The frame RAM (its contents change, so only timed)
    ram_ns = []
    for _ in range(reads):
        t = time.monotonic_ns()
        try:
            read_words(0x0400, 832)
        except OSError:
            errors += 1
            continue
        ram_ns.append(time.monotonic_ns() - t)
    feed()

    # The encoder
    angles = []
    for _ in range(10 * reads):
        try:
            angles.append(hardware.read_raw_angle())
        except OSError:
            errors += 1
    feed()

    log.info(
        "bench_i2c: %d Hz: EEPROM %d reads, %d differed (%d words), %.1f ms each; "
        "frame RAM %.1f ms",
        frequency,
        len(eeprom_ns),
        bad_reads,
        bad_words,
        _ms(sum(eeprom_ns) / max(len(eeprom_ns), 1)),
        _ms(sum(ram_ns) / max(len(ram_ns), 1)),
    )
    log.info(
        "bench_i2c: encoder %d reads, raw angle %s..%s; %d I2C errors",
        len(angles),
        min(angles) if angles else "-",
        max(angles) if angles else "-",
        errors,
    )
    log.info("bench_i2c: done in %.1f s", _ms(time.monotonic_ns() - start) / 1000)
