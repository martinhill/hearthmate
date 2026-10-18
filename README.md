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
- Adafruit MLX90640 driver, forked in this repo (`adafruit_mlx90640.py`, overrides the library copy)
- Plus each library's respective dependencies (`requirements.txt`)

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

- `scripts/deploy.sh`: install or update the firmware over the web workflow. `-n` only shows
  what differs, `--libs` also installs `requirements.txt`. After uploading it sends the reload
  command over MQTT (broker credentials in `~/.config/mosquitto_pub`) and reports whether the
  device resumed the burn; see below.
- `scripts/tmux.sh`: watch the device log and state topics over MQTT (iTerm2: `tmux -CC`).
  Broker credentials go in `~/.config/mosquitto_sub`.
- `simulation/`: stove models fitted to recorded burns and closed-loop tests of `temp_control`
  (run with `uv run --project marimo`). `marimo/` has the burn data viewer.
- `service/`: the StoveLink service that records thermal frames and the device log to HDF5.

## Restarts during a burn

A soft reboot (a deploy, the reload command or HA's Reload button, Ctrl-D in the REPL) resumes
the burn where it was: mode, controller phase and memory, burn timer (so the burn file doesn't
split), target, Closing Duration and step parameters. The device saves that state to the
retained topic `<MQTT_TOPIC>/controller/saved` on every phase change and vent move, and at least
every 30 s. At boot it resumes only if CircuitPython reports a reload rather than a hard start,
the device clock shows the state is under 5 minutes old (the clock restarts at 2000-01-01 after
a hard reset), and the vent is where it was saved. Otherwise, e.g. after a power cut, it starts
in idle as before. See `burn_state.py`; `simulation/restart.py` tests it.

A hardware watchdog resets the board if the main loop stops for `WATCHDOG_TIMEOUT` seconds
(120 by default): the board has frozen for good inside a network call while still answering
ping. The burn resumes after a watchdog reset too, if the device clock survived it; otherwise
it starts in idle. To test it on a cold stove, send the `test_watchdog` command, which stops the
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
is the CircuitPython default), `AUTORELOAD` (`off` by default) and `WATCHDOG_TIMEOUT`.

## TODO

- Main loop stalls of ~60 s, a few per day, since at least Dec 2025, blocking the whole board
  (seen inside mqtt_client.loop(), time.sleep() and the camera driver's arithmetic). They end
  at the same second of the minute, also with `WIFI_POWER_MANAGEMENT = "none"`. Suspected cause:
  web workflow pages (code editor, serial console) left open in background browser tabs, whose
  timers browsers throttle to once a minute. Since 18 Oct 2026 all such tabs are closed: check
  whether "Main loop stalled" / "MQTT loop gap" warnings stop. Don't leave those pages open.
- Corrupted thermal frames and I2C hangs: a race in the forked MLX90640 driver or the wiring.
  Bad frames are kept out of control; the STEMMA QT power-cycle recovery is untested on hardware.
- TMP36 drifts down and then reads −35.8°C (the ADC floor, ~0 V) for minutes at a time
  (17 Oct 2026, 13:30–17:40): likely a loose supply or ground connection.
- Explicit indication in HA and LED when mock hardware is selected due to the I2C scan result
- Detect a stalled or slipping motor and report it in HA: per-move position error is already
  published, but nothing acts on it, and nothing tracks slippage over a full open→closed travel
  ((steps_needed - actual_steps) / steps_needed). End stops make a plain exception unsafe.
- `temp_control` closes quickly early in the burn; check against more burns.
- Type-check the firmware (e.g. pyright in the root uv environment)
