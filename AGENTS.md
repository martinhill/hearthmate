# Agent Guidelines for Hearthmate

## Project Overview
CircuitPython project for an automated wood stove air vent controller using ESP32 Feather v2, stepper motor, and AS5600 position sensor. Main files: `code.py`, `state_machine.py`, `testing.py`, `hardware.py`.

## Build & Test Commands
- No traditional build system; runs directly on CircuitPython device
- Device upload: Copy .py files to device's `/lib` or root via REPL or file transfer

## Code Style Guidelines

**Imports:** Group standard library (time, os, wifi), Adafruit libraries, then local modules. One import per line.

**Formatting:** 4-space indentation, max 100 chars per line, PEP 8 style where CircuitPython allows.

**Types:** Use docstrings with parameter descriptions; optional type hints in comments for CircuitPython compatibility (e.g., `# angle: float`).

**Naming:** snake_case for functions/variables, PascalCase for classes. Descriptive names: `read_raw_angle()`, `current_step`, not abbreviated forms.

**Error Handling:** Catch specific exceptions (e.g., `MMQTTException`); print errors with context. Handle I2C failures gracefully (see `get_hardware()` fallback to MockHardware).

**State Machine:** Use State class pattern from state_machine.py—implement `enter()`, `update()`, `exit()` methods. Store shared data in `machine.data` dict.

**Comments:** Document non-obvious logic, especially encoder angle calculations and motion timing. Use TODO markers sparingly.

**Hardware Abstraction:** Always use Hardware interface for motor/sensor access. MockHardware class enables testing without physical hardware.
