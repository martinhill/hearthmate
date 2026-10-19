# MLX90640 Frame Time: Calculation vs I2C

Measured on the board on 19 Oct 2026 (cold stove, Ta 23.5°C, chess mode, no deviating pixels)
with the `bench_camera` and `bench_i2c` commands. Run them from the desktop:

```bash
uv run --with numpy python scripts/bench_mlx90640.py device   # bench_camera, checks the capture
uv run --with numpy python scripts/bench_mlx90640.py bus      # bench_i2c
uv run --with numpy python scripts/bench_mlx90640.py check    # desktop only: both drivers agree
```

Each blocks the main loop for 5–17 s (the watchdog is fed). `device` saves the raw capture
(EEPROM and both subpages) to `reference/`; `check CAPTURE` recalculates it on the desktop.

## Questions

1. Would sending raw frames to the StoveLink service and calculating the temperatures there
   (an old README TODO) save meaningful loop time?
2. Is the ulab driver (`numpy_mlx90640.py`) worth switching to?

Both: **no**. The time went to the I2C bus, which ran at 100 kHz.

## Results

A frame is two subpages; each subpage is one 832-word frame RAM read plus one `_CalculateTo()`.

| Per subpage | 100 kHz | 400 kHz |
|---|---|---|
| Frame RAM read, per-pixel driver (`struct.unpack` into a list) | 168 ms | 55 ms |
| Frame RAM read, ulab driver (`np.frombuffer`) | 156 ms | 44 ms |
| Raw 832-word read (`bench_i2c`) | 154 ms | 41.5 ms |
| `_CalculateTo()`, per-pixel | 123 ms | 123 ms |
| `_CalculateTo()`, ulab | 71 ms | 71 ms (one run 143 ms) |

| Per frame | 100 kHz | 400 kHz |
|---|---|---|
| `getFrame()`, per-pixel (incl. waiting for the sensor) | 704 ms | 375–481 ms |
| `getFrame()`, ulab | 645 ms | 377–611 ms |

- The two drivers agree to 0.0002°C on the device (float32) and exactly on the desktop in
  float64.
- ulab allocates ~174 KB per subpage (temporaries of 768 floats) against ~250 bytes for the
  per-pixel loop, plus 29 KB resident for the calibration arrays and ~600 ms at init. The
  heap pressure is what `THERMAL_CAMERA_MEMORY_FIX.md` warns about, and its timing is
  inconsistent (one 400 kHz run took 143 ms per subpage).
- At 400 kHz `bench_i2c`: 50 EEPROM reads matched the init copy word for word, 500 encoder
  reads were steady, no I2C errors. No camera or I2C errors in normal running since.

## Conclusions

- The whole calculation is ~250 ms per frame, ~2.5% of the 10 s loop. Moving it to the service
  would save that at the cost of a second frame format and the device's own max-temperature
  reading, which `temp_control` needs locally. Not worth it.
- ulab halves the calculation, but saves ~100 ms per frame for ~350 KB of allocation per frame.
  Kept in the repo for reference (`numpy_mlx90640.py`, a subclass of the forked driver), not
  used by `thermal_camera.py`.
- The bus clock was the real cost: `I2C_FREQUENCY = 400000` in `settings.toml` cuts each
  subpage read from ~165 ms to ~50 ms, ~230 ms per frame. Don't go higher: the MLX90640 supports
  1 MHz, but its datasheet limits EEPROM reads, done at every driver init (including camera
  recovery), to 400 kHz. `busio.I2C` doesn't report its clock, so the firmware logs the
  setting.

## Driver fix found on the way

`brokenPixels` and `outlierPixels` were class attributes of the forked driver, appended to at
each init. A second `MLX90640` (the camera recovery after a power cycle) found every deviating
pixel twice and raised "Adjacent broken pixels". They are per instance now. This sensor has no
deviating pixels, so it never triggered here; `scripts/bench_mlx90640.py check` covers it.
