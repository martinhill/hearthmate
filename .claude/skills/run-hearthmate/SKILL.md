---
name: run-hearthmate
description: Run, simulate, test and watch Hearthmate, the wood stove air vent controller. Use when asked to run or test the vent control logic (temp_control, timed_close), simulate a burn, plot or screenshot a simulated burn, replay recorded burns, check the firmware and StoveLink service, or check status / watch / tail the logs of the live stove device.
---

Hearthmate's firmware runs only on the ESP32 under CircuitPython, and the live system is a real
wood stove. An agent drives it with `.claude/skills/run-hearthmate/driver.py`:
- **simulate** the firmware's vent control on the desktop: the real `VentCloser` state machine and
  `TemperatureTargetFunction` against a stove model fitted to recorded burns, with fake hardware;
- **observe** the live device read-only over MQTT, and the current burn's log on the StoveLink host.

All paths are relative to the repo root; run every command from there.

## Prerequisites

This is set up on a macOS desktop. Needed:
- `uv`, `mosquitto_sub`, and `ssh` access to the StoveLink host (`ssh stovelink` without a password).
- The burn data share (`/Volumes/Burnie`) mounted.
- Broker credentials in `~/.config/mosquitto_sub`: lines `-u <user>` and `-P <password>`, `chmod 600`.
  The person sets this up; agents must not write passwords.

## Setup

```bash
cp .env.example .env
```

`.env` holds `HEARTHMATE_TOPIC` (`burnie`), `HEARTHMATE_MQTT_HOST` (`homeassistant`),
`BURNIE_DATA` (`/Volumes/Burnie`), `STOVELINK_HOST` (`stovelink`) and `STOVELINK_DATA`
(`/mnt/burnie`). Environment variables override it. The first `uv run` syncs the marimo project's
environment, which the driver uses.

## Run (agent path)

Simulate a burn and save a plot (the "screenshot"; `simulation/*.png` is gitignored), then look at it:

```bash
uv run --project marimo python .claude/skills/run-hearthmate/driver.py sim --quiet --png simulation/sim.png
```

It prints each controller phase change and the vent reaching closed, e.g.
`3.7 min phase=regulate ... vent=0.000 closed`, ending `done: peak 390C, final phase coals`. Drop
`--quiet` to see the controller's own INFO lines, in the same format as on the device.

Scenarios that exercise specific paths:

```bash
uv run --project marimo python .claude/skills/run-hearthmate/driver.py sim --load 0.4 --kindling 30 --hours 2 --quiet --png simulation/sim-kindling.png
```

```bash
uv run --project marimo python .claude/skills/run-hearthmate/driver.py sim --outage 3:15 --hours 1
```

| command | what it does |
|---|---|
| `sim` | `--model 2\|3`, `--burn N` (0-89, a fitted recorded burn), `--load` fuel multiplier, `--kindling MIN`, `--outage START:END` (camera loss, minutes), `--close-time S`, `--hours`, `--png`, `--quiet` |
| `replay [--fair]` | recorded burns through the stall / coal-stage classifier (~2 min) |
| `check` | firmware syntax check + StoveLink service tests |
| `status [--seconds N]` | live device: latest state-topic values seen in N s (read-only) |
| `watch [--minutes N]` | live device: controller transitions, overrides, warnings, errors (read-only) |
| `burnlog [--lines N] [--file F]` | current burn's device log via ssh, or an archived `YYYYMM/DD/*.log` |

Live device, read-only:

```bash
uv run --project marimo python .claude/skills/run-hearthmate/driver.py status
```

```bash
uv run --project marimo python .claude/skills/run-hearthmate/driver.py watch --minutes 0.5
```

```bash
uv run --project marimo python .claude/skills/run-hearthmate/driver.py burnlog --lines 4
```

```bash
uv run --project marimo python .claude/skills/run-hearthmate/driver.py burnlog --file 202610/17/20261017-08.log --lines 3
```

## Run (human path)

`scripts/tmux.sh` opens a tmux window `mqtt` with the filtered device log, the state topics and a
shell, adding it to the session if one exists (e.g. created by an iTerm2 profile with `tmux -CC`).
In iTerm2 it attaches with `tmux -CC`, unless iTerm2 already shows the session. Agents can run it
detached and read the panes; give it a throwaway session name so a human's session isn't touched:

```bash
HEARTHMATE_SESSION=hm-agent TMUX= scripts/tmux.sh </dev/null >/dev/null 2>&1; sleep 2; tmux capture-pane -p -J -t hm-agent:0.0 | grep -v '^$' | tail -3; tmux kill-session -t hm-agent
```

## Test

```bash
uv run --project marimo python .claude/skills/run-hearthmate/driver.py check
```

Expect `firmware syntax: 14/14 files ok`, both service suites passing (8/8, 4/4) and
`restart: 123/123 restart checks passed` (soft reboots in every controller phase; ~45 s).
Simulation regression across both stove models, parameter variants and sensor noise (~30 s):

```bash
cd simulation && uv run --project ../marimo python final.py
```

## Gotchas

- **The repo's `logging.py` shadows the stdlib `logging`** and imports `adafruit_logging`. The repo
  root must come *last* on `sys.path`, and `uv run --project marimo python -c "..."` run from the
  repo root fails with `No module named 'adafruit_logging'`. The driver handles this; scripts in
  `simulation/` append the repo root for the same reason.
- **Only some firmware modules run on CPython.** `temperature_target`, `vent_closer`,
  `state_machine` and `airvent` do; `code.py`, `hardware.py` and `thermal_camera.py` need
  CircuitPython (`board`, `wifi`, `ulab`). `check` only syntax-checks those.
- **The live driver is read-only on purpose.** Commands to `burnie/command` or
  `burnie/air_vent/set` move the vent on a real fire. `mosquitto_pub` also reads its own
  `~/.config/mosquitto_pub`, which isn't set up.
- **`status` shows only some topics.** The device publishes most HA states only when they change,
  and nothing is retained. Seeing 4/11 topics is normal; a quiet `watch` (just `Timed out` at the
  end) means nothing happened.
- **The share serves stale contents of files the StoveLink service still has open.** On the Mac,
  `current_cycle.log` showed the right size but old text. `burnlog` reads it over ssh instead.
  Archived files read fine. `current_cycle.h5` can't be opened at all while the service holds it.
- **The device clock reads 2000-01-01** (no RTC). Match device log lines to HDF5 frames by the `t=`
  / `combustion_time` and `seq` columns, not by time.
- **Deploying reloads the device.** `scripts/deploy.sh -n` (read-only) shows what differs from
  the device; uploading (`scripts/deploy.sh`) is for the person to run: it ends with the MQTT
  `reload` command, and the device resumes the burn from the retained
  `burnie/controller/saved` state (`burn_state.py`). Firmware from before that resume reloads on
  every upload and starts in `idle`. `wwshell` prints the web workflow password in its
  "Found device" line; the script strips it.
- **Autoreload is off while the firmware runs** and CircuitPython keeps that across soft
  reloads, so `code.py` turns it back on in every exit path. Keep it that way, or a crashed
  deploy can only be recovered by power-cycling the board.
- **`replay` is slow (~2 min)** because it reads every HDF5 burn file over SMB.

## Troubleshooting

- **`No module named 'adafruit_logging'`**: something imported the repo's `logging.py`. Run the
  driver as shown, not with `-c` from the repo root.
- **`zsh: command not found: mosquitto_sub` in tmux panes**: the tmux server's PATH lacks the
  install location. `scripts/tmux.sh` resolves the full path; installing mosquitto via nix-darwin
  puts it on every PATH.
- **`Connection error: Connection Refused: not authorised.`**: no `~/.config/mosquitto_sub`
  credentials.
- **`burnlog` shows old lines, or a stray blank entry**: you're reading the share's cached copy;
  set `STOVELINK_HOST` in `.env`.
