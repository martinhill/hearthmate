# Hearthmate

An experimental robot that controls a wood stove air vent to achieve optimal combustion. Optimal combustion maximizes heat retention and minimizing creosote buildup.

## Hardware

- [Adafruit ESP32 Feather v2](https://learn.adafruit.com/adafruit-esp32-feather-v2/)
- [Adafruit Stepper + DC Motor FeatherWing](https://learn.adafruit.com/adafruit-stepper-dc-motor-featherwing/)
- 2.3 Kg*cm NEMA16 Bipolar stepper
- [ams AS5600 position sensor](https://ams-osram.com/products/sensor-solutions/position-sensors/ams-as5600-position-sensor)
- MLX 90640 thermal 32x24 IR array
- TMP36 analog temperature sensor
- 5:1 gear ratio belt and pulley connecting motor to drive shaft
- mechanical linkage from drive shaft to vent control lever arm

## Software stack

- [CircuitPython 10.3.x](https://docs.circuitpython.org/en/stable/docs/index.html)
- [Adafruit Motorkit library](https://docs.circuitpython.org/projects/motorkit/en/latest/)
- [Adafruit motor library](https://docs.circuitpython.org/projects/motor/en/latest/)
- [Adafruit MiniMQTT library](https://docs.circuitpython.org/projects/minimqtt/en/latest/)
- Adafruit MLX90640 driver, forked in this repo (`firmware/adafruit_mlx90640.py`, overrides the library
  copy). `firmware/numpy_mlx90640.py` is a ulab version of its calculation, benchmarked and not used; see
  [docs_by_agents/MLX90640_PERFORMANCE.md](docs_by_agents/MLX90640_PERFORMANCE.md).
- Plus each library's respective dependencies (`firmware/requirements.txt`)

## Operating modes

- `timed_close`: closes the vent linearly over the Closing Duration.
- `temp_control`: closes the vent as fast as the fire allows while holding the camera's max
  temperature near a target (300°C), then closes gradually on coals. See
  [docs_by_agents/TEMPERATURE_TARGET_MODE.md](docs_by_agents/TEMPERATURE_TARGET_MODE.md).

Either mode starts when the vent is fully reopened after being closed (`AUTO_MODE` in
`settings.toml`, default `timed_close`) or from the Home Assistant mode select.

## Desktop setup

```bash
cp .env.example .env    # then set CIRCUP_WEBWORKFLOW_PASSWORD
uv sync                 # editor environment: Blinka + CircuitPython libraries, circup
```

- `firmware/`: the CircuitPython code. Its contents go in the device's root directory
  (CIRCUITPY), with `settings.toml` and the libraries in `lib/`.

- `scripts/deploy.sh`: install or update the firmware over the web workflow. `-n` only shows
  what differs, `--libs` also installs `firmware/requirements.txt`. After uploading it sends the reload
  command over MQTT (broker credentials in `~/.config/mosquitto_pub`) and reports whether the
  device resumed the burn; see below.
- `scripts/tmux.sh`: watch the device log and state topics over MQTT (iTerm2: `tmux -CC`).
  Broker credentials go in `~/.config/mosquitto_sub`.
- `simulation/`: stove models fitted to recorded burns and closed-loop tests of `temp_control`
  (run with `uv run --project marimo`). `marimo/` has the burn data viewer.
- `service/`: the StoveLink service that records thermal frames and the device log to HDF5.
- `scripts/bench_mlx90640.py`: benchmark the camera calculation and the I2C bus on the device
  (`device`, `bus`), or check the drivers against each other on the desktop (`check`).

## Restarts during a burn

A soft reboot (a deploy, the reload command or HA's Reload button, Ctrl-D in the REPL) resumes
the burn where it was: mode, controller phase and memory, burn timer (so the burn file doesn't
split), target, Closing Duration and step parameters. The device saves that state to the
retained topic `<MQTT_TOPIC>/controller/saved` on every phase change and vent move, and at least
every 30 s. At boot it resumes only if CircuitPython reports a reload rather than a hard start,
the device clock shows the state is under 5 minutes old (the clock restarts at 2000-01-01 after
a hard reset), and the vent is where it was saved. Otherwise, e.g. after a power cut, it starts
in idle as before. See `firmware/burn_state.py`; `simulation/restart.py` tests it.

A hardware watchdog resets the board if the main loop stops for `WATCHDOG_TIMEOUT` seconds
(120 by default): the board has frozen for good inside a network call while still answering
ping. The burn resumes after a watchdog reset too, if the device clock survived it; otherwise
it starts in idle. Tested on the board on 18 Oct 2026: reset 2 minutes after the main loop
stopped, a normal boot (not safe mode), the clock survived, and `temp_control` resumed after
142 s. To test it on a cold stove, send the `test_watchdog` command, which stops the
main loop:

```bash
mosquitto_pub -h homeassistant -t burnie/command -m test_watchdog
```

Autoreload is off while the firmware runs (`AUTORELOAD`), so a deploy reloads once with all
files in place. It is turned back on whenever the main loop exits, so a fixed upload still
reloads code that crashed.

`settings.toml` on the device holds WiFi, MQTT and web workflow credentials and isn't in the
repo. Firmware settings are read with `os.getenv` there, e.g. `AUTO_MODE`, `VENT_CLOSE_TIME`,
the `TT_*` temperature control settings, `WIFI_POWER_MANAGEMENT` (`none` by default, `min`
is the CircuitPython default), `AUTORELOAD` (`off` by default), `WATCHDOG_TIMEOUT` and
`I2C_FREQUENCY` (100 kHz when unset; the board runs at 400000, the camera's limit for EEPROM
reads, which saves ~230 ms per thermal frame).

## TODO

- Main loop stalls of ~60 s, a few per day, since at least Dec 2025, blocking the whole board
  (seen inside mqtt_client.loop(), time.sleep() and the camera driver's arithmetic), and twice on
  18 Oct 2026 a permanent freeze (the board still answered ping). Cause: browsers connected to
  the web workflow. The stalls ended at the same second of the minute while Safari/Chrome tabs
  had its pages open; with them closed, the next stall and freeze came when Chrome connected to
  the board again and kept an idle connection open. Short requests (curl, wwshell, deploy.sh)
  haven't caused any. Don't open the web workflow in a browser; the watchdog recovers a freeze.
  Worth reporting to CircuitPython with a minimal reproduction.
- Corrupted thermal frames and I2C hangs: a race in the forked MLX90640 driver or the wiring.
  Bad frames are kept out of control; the STEMMA QT power-cycle recovery is untested on hardware
  (its driver re-init failed on a sensor with deviating pixels until 19 Oct 2026). The bus runs
  at 400 kHz since 19 Oct 2026: watch whether these change.
- TMP36 drifts down and then reads −35.8°C (the ADC floor, ~0 V) for minutes at a time
  (17 Oct 2026, 13:30–17:40): likely a loose supply or ground connection.
- Explicit indication in HA and LED when mock hardware is selected due to the I2C scan result
- Detect a stalled or slipping motor and report it in HA: per-move position error is already
  published, but nothing acts on it, and nothing tracks slippage over a full open→closed travel
  ((steps_needed - actual_steps) / steps_needed). End stops make a plain exception unsafe.
- `temp_control` closes quickly early in the burn; check against more burns.
- Type-check the firmware (e.g. pyright in the root uv environment)
