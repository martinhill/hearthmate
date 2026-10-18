# Stove simulation & controller tuning

Desktop-only tools (CPython, not deployed to the device) for fitting stove models to recorded
StoveLink burns and tuning `TemperatureTargetFunction`. The simulator imports the real
`temperature_target.py` and `vent_closer.py` from the repo root, so it exercises the device code.

Run with the marimo environment, adding scipy for fitting:

```bash
cd simulation
uv run --project ../marimo --with scipy python fit.py 3    # refit plant_v3 (~6 min), writes fit_all_v3.json
uv run --project ../marimo python ensemble.py quick         # linear vs temp target on both models
uv run --project ../marimo python tune.py                   # tuning grid search
uv run --project ../marimo python final.py                  # full evaluation + final_v*.png plots
uv run --project ../marimo python integration.py 40         # VentCloser state machine with fake hardware
uv run --project ../marimo python replay_stalls.py fair     # stall / coal-stage classifier on recorded burns
```

Data comes from `$BURNIE_DATA` (default `/Volumes/Burnie`). Files that can't be opened are skipped,
such as the cycle StoveLink is currently writing.

| File | Purpose |
|---|---|
| `data.py` | Load HDF5 cycles, filter chessboard frames, select fit-able burns |
| `plant_v2.py` | Fuel-dominated slow model |
| `plant_v3.py` | Air/fuel-limited burn rate with fast flame radiation term |
| `fit.py` | Least-squares fit of global + per-burn (fuel load, initial fire) parameters |
| `closedloop.py` | Closed-loop simulation, metrics, plant parameter variants |
| `ensemble.py`, `tune.py`, `final.py`, `traj.py` | Evaluation, grid search, plots |
| `integration.py` | Runs the real `VentCloser` sub-state machine against the model |
| `replay_stalls.py` | Replays recorded burns through the stall / coal-stage classifier and compares with your manual rescues |
