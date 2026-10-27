# Agent Guidelines for Hearthmate

## Project Overview
CircuitPython 10.3.x project for an automated wood stove air vent controller using ESP32 Feather v2, stepper motor, and AS5600 position sensor.

## Layout
- `firmware/`: the CircuitPython code, deployed to the device's root directory (CIRCUITPY).
  Main files: `code.py`, `state_machine.py`, `hw_test.py`, `hardware.py`; device libraries in
  `requirements.txt`. Modules import each other by bare name, as on the device.
- `simulation/`: desktop tools (CPython) that append `firmware/` to `sys.path`. `marimo/`: the
  burn data viewer.
- `service/`: the StoveLink service. `scripts/`: deploy, monitoring and bench tools.

## Build & Test Commands
- No traditional build system; runs directly on CircuitPython device
- Desktop environment: `uv sync` (root `pyproject.toml`): Blinka and the CircuitPython libraries for
  editor completion, circup and python-dotenv. Simulation/notebooks use `marimo/`, the service `service/`
- Device upload: `scripts/deploy.sh` (`-n` shows what differs, `--libs` also installs
  `firmware/requirements.txt` with circup). It reloads the device once; a burn resumes after the reload (`firmware/burn_state.py`).
- Controller state that must survive a soft reboot goes in `save_state()`/`restore_state()` of the
  vent functions; `simulation/restart.py` checks restarts in every phase. Restore fields added
  later with `state.get(key, default)`: a deploy mid-burn restores a state saved by the previous
  firmware.

## Code Style Guidelines

**Imports:** Group standard library (time, os, wifi), Adafruit libraries, then local modules. One import per line.

**Formatting:** 4-space indentation, max 100 chars per line, PEP 8 style where CircuitPython allows.

**Types:** Use docstrings with parameter descriptions; optional type hints in comments for CircuitPython compatibility (e.g., `# angle: float`).

**Naming:** snake_case for functions/variables, PascalCase for classes. Descriptive names: `read_raw_angle()`, `current_step`, not abbreviated forms.

**Error Handling:** Catch specific exceptions (e.g., `MMQTTException`); print errors with context. Handle I2C failures gracefully (see `get_hardware()` fallback to MockHardware).

**State Machine:** Use State class pattern from state_machine.py—implement `enter()`, `update()`, `exit()` methods. Store shared data in `machine.data` dict.

**Comments:** Document non-obvious logic, especially encoder angle calculations and motion timing. Use TODO markers sparingly.

**Hardware Abstraction:** Always use Hardware interface for motor/sensor access. MockHardware class enables testing without physical hardware.

## CircuitPython Version Notes

**Minimum version: 10.2.0.** As of CircuitPython 10.2.0, `os.getenv()` always returns a `str`, even for unquoted integer values in `settings.toml` (e.g. `VENT_CLOSE_TIME = 2700` comes back as `'2700'`). All numeric env reads must be wrapped in `int()` or `float()`. Use `supervisor.get_setting()` if you need typed values, but prefer the cast-at-read pattern for consistency with the existing codebase.

**I2C:** The encoder, motor FeatherWing and MLX90640 share one bus, clocked by `I2C_FREQUENCY`
(`firmware/hardware.py`; 400 kHz on the board). Don't exceed 400 kHz: the MLX90640 reads its EEPROM at
every driver init. `busio.I2C` has no `frequency` attribute, so log the setting instead.

**ulab:** Each array operation allocates a new array; in a per-frame path that is heap pressure
(see `docs_by_agents/MLX90640_PERFORMANCE.md`, `THERMAL_CAMERA_MEMORY_FIX.md`). `tobytes()`
returns a view that minimqtt won't publish: wrap it in `bytes()` or prepend `b""`.
