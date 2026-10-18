"""Resume a burn after a soft reboot (a deploy, the reload command or Ctrl-D) from controller state
saved in a retained MQTT message, and after a watchdog reset of a hung main loop. After any
other hard reset the device starts in idle as before.

The saved state includes the device clock (time.time()). It keeps running through a soft reboot
and restarts at 2000-01-01 after a hard reset, so the difference to the saved clock is the time
the device was down. Together with supervisor.runtime.run_reason (STARTUP on a hard reset) it
decides whether to resume:
  - the run reason is a reload, or the reset was the watchdog's (not a power-up or brownout),
  - the saved state is between 0 and max_age seconds old by the device clock, and
  - the vent is where it was saved (nobody moved it by hand meanwhile).
Otherwise the device starts in idle and the retained message is cleared, so a stale state can't
come back later.
"""

import json
import time

try:
    import supervisor
    import microcontroller
except ImportError:
    supervisor = None
    microcontroller = None

try:
    import adafruit_logging as logging
except ImportError:
    import logging

logger = logging.getLogger(__name__)

SAVE_VERSION = 1
MODES = ("timed_close", "temp_control")
# Starts that may resume: soft reboots, and a watchdog reset of a hung main loop (see code.py)
RESUMABLE = ("AUTO_RELOAD", "SUPERVISOR_RELOAD", "REPL_RELOAD", "WATCHDOG")


def reset_reason():
    "Why the microcontroller last reset, e.g. POWER_ON, WATCHDOG, BROWNOUT, RESET_PIN"
    if microcontroller is None:
        return "POWER_ON"
    # e.g. "microcontroller.ResetReason.WATCHDOG"
    return str(microcontroller.cpu.reset_reason).split(".")[-1]


def run_reason():
    """Why CircuitPython started this run: AUTO_RELOAD, SUPERVISOR_RELOAD or REPL_RELOAD for a
    soft reboot; WATCHDOG for a hard start after a watchdog reset; otherwise STARTUP"""
    if supervisor is None:
        return "STARTUP"
    # e.g. "supervisor.RunReason.AUTO_RELOAD"
    reason = str(supervisor.runtime.run_reason).split(".")[-1]
    if reason == "STARTUP" and reset_reason() == "WATCHDOG":
        return "WATCHDOG"
    return reason


def active_mode(machine):
    "The running mode (timed_close or temp_control), also while a manual move is pushed over it"
    for name in machine.state_stack + [machine.current_state]:
        if name in MODES:
            return name
    return None


class BurnStateStore:
    """Saves the controller state to a retained MQTT topic and restores it at boot.

    Args:
        mqtt_client: MiniMQTT client
        topic: retained topic for the saved state
        functions: {mode name: vent function} for the settings saved in every state
        hardware: Hardware, for the step parameters set from Home Assistant
        clock: device clock, time.time
        save_interval: save at least this often (s)
        min_interval: save at most this often (s), except on a mode or phase change
        position_change: save when the vent has moved this much (fraction) since the last save
        max_age: resume only from a state at most this old (s)
        position_tolerance: resume only if the vent is within this of the saved position
    """

    def __init__(
        self,
        mqtt_client,
        topic,
        functions,
        hardware,
        clock=time.time,
        save_interval=30,
        min_interval=5,
        position_change=0.01,
        max_age=300,
        position_tolerance=0.05,
    ):
        self.mqtt_client = mqtt_client
        self.topic = topic
        self.functions = functions
        self.hardware = hardware
        self.clock = clock
        self.save_interval = save_interval
        self.min_interval = min_interval
        self.position_change = position_change
        self.max_age = max_age
        self.position_tolerance = position_tolerance
        self.last_save = None  # (clock, key, vent position)
        self.received = None

    ## Saving

    def snapshot(self, machine, sequence_id):
        "The state to save, as a dict"
        mode = active_mode(machine)
        state = {
            "v": SAVE_VERSION,
            "clock": self.clock(),
            "mode": mode,
            "vent": round(machine.data["vent"].get_position(), 4),
            "seq": sequence_id,
            "settings": {
                "target": self.functions["temp_control"].target,
                "time_range": self.functions["timed_close"].time_range,
                "step_params": self.hardware.step_params,
            },
        }
        if mode:
            closer = machine.states[mode]
            state["vent_closed"] = closer.machine.data["vent_closed"]
            state["function"] = closer.machine.data["function"].save_state()
        return state

    def _key(self, machine):
        "What changing triggers an immediate save: the mode and the temp_control phase"
        mode = active_mode(machine)
        phase = getattr(machine.states[mode].machine.data["function"], "phase", None) if mode else None
        return (mode, phase)

    def maybe_save(self, machine, sequence_id):
        "Save if the mode or phase changed, the vent moved, or save_interval has passed"
        now = self.clock()
        key = self._key(machine)
        position = machine.data["vent"].get_position()
        if self.last_save is not None:
            saved_time, saved_key, saved_position = self.last_save
            elapsed = now - saved_time
            moved = abs(position - saved_position) >= self.position_change
            if key == saved_key and elapsed < self.save_interval and not (
                moved and elapsed >= self.min_interval
            ):
                return False
        self.save(machine, sequence_id)
        return True

    def save(self, machine, sequence_id):
        "Publish the state now"
        state = self.snapshot(machine, sequence_id)
        self.mqtt_client.publish(self.topic, json.dumps(state), retain=True)
        self.last_save = (state["clock"], self._key(machine), state["vent"])

    def clear(self):
        "Remove the retained state from the broker"
        self.mqtt_client.publish(self.topic, "", retain=True)
        self.last_save = None

    ## Restoring

    def on_message(self, message):
        "MQTT handler for the retained topic while fetching"
        self.received = message

    def fetch(self, timeout=3.0):
        """Wait up to timeout seconds for the retained state after subscribing to it.
        The caller routes self.topic to on_message. Returns the parsed state or None."""
        self.received = None
        self.mqtt_client.subscribe(self.topic)
        deadline = time.monotonic() + timeout
        while True:
            self.mqtt_client.loop(timeout=0.2)
            if self.received is not None or time.monotonic() >= deadline:
                break
        self.mqtt_client.unsubscribe(self.topic)
        if not self.received:
            return None
        try:
            return json.loads(self.received)
        except ValueError:
            logger.warning("Ignoring unreadable saved burn state")
            return None

    def check(self, saved, reason, vent_position):
        """Decide whether to resume from saved.

        Args:
            saved: state from fetch(), or None
            reason: run_reason()
            vent_position: current vent position from the encoder

        Returns:
            (bool, str): resume or not, and why, for the log
        """
        if saved is None:
            return False, "no saved state"
        if saved.get("v") != SAVE_VERSION:
            return False, "saved state has version %s, expected %d" % (saved.get("v"), SAVE_VERSION)
        if reason not in RESUMABLE:
            return False, "hard reset (%s, %s)" % (reason, reset_reason())
        downtime = self.clock() - saved["clock"]
        if downtime < 0:
            return False, "device clock is behind the saved state (hard reset)"
        if downtime > self.max_age:
            return False, "saved state is %ds old" % downtime
        if saved["mode"] and abs(vent_position - saved["vent"]) > self.position_tolerance:
            return False, "vent moved from %.3f to %.3f" % (saved["vent"], vent_position)
        return True, "%s after %ds (%s)" % (saved["mode"] or "idle", downtime, reason)

    def restore(self, machine, saved):
        """Apply a state that passed check(): settings, then the mode and its function.
        Returns the seconds the device was down."""
        downtime = self.clock() - saved["clock"]
        settings = saved["settings"]
        self.functions["temp_control"].target = settings["target"]
        for function in self.functions.values():
            function.time_range = settings["time_range"]
        self.hardware.step_params.update(settings["step_params"])

        mode = saved["mode"]
        if mode is None:
            machine.set_state("idle")
            return downtime
        machine.states[mode].machine.data["function"].restore_state(saved["function"], downtime)
        machine.restore_state(mode, saved["vent_closed"])
        return downtime
