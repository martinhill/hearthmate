# Agent Guidelines for Hearthmate

## Project Overview
CircuitPython 10.3.x project for an automated wood stove air vent controller using ESP32 Feather v2, stepper motor, and AS5600 position sensor. Main files: `code.py`, `state_machine.py`, `testing.py`, `hardware.py`.

## Build & Test Commands
- No traditional build system; runs directly on CircuitPython device
- Desktop environment: `uv sync` (root `pyproject.toml`): Blinka and the CircuitPython libraries for
  editor completion, circup and python-dotenv. Simulation/notebooks use `marimo/`, the service `service/`
- Device upload: `scripts/deploy.sh` (`-n` shows what differs, `--libs` also installs `requirements.txt`
  with circup). It reloads the device, so deploy between burns.

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
