import os
import time

try:
    import adafruit_logging as logging
except ImportError:
    import logging

logger = logging.getLogger(__name__)

MQTT_TIMEOUT: float = float(os.getenv("MQTT_TIMEOUT", "0.1"))


class State:
    """Base class for states in the state machine"""

    def __init__(self, name):
        self.name = name

    def enter(self, machine):
        """Called when entering the state"""
        pass

    def resume(self, machine):
        """Called when resuming the state from the stack"""
        self.enter(machine)

    def exit(self, machine):
        """Called when exiting the state"""
        pass

    def update(self, machine):
        """Called periodically to update the state"""
        pass

    def handle_move_request(self, machine, target_position):
        """
        Handle a request to move the vent to a specific position.
        Returns True if the request was accepted/handled, False otherwise.
        """
        return False


class StateMachine:
    """Simple state machine implementation"""

    def __init__(self, initial_state=None, mqtt_timeout=MQTT_TIMEOUT):
        self.states = {}
        self.current_state = None
        self.previous_state = None
        self.state_stack = []
        self.data = {}  # Shared data between states
        self.mqtt_timeout = mqtt_timeout
        self._last_loop_time = None  # Track last mqtt_loop() call time

        if initial_state:
            self.add_state(initial_state)
            self.set_state(initial_state.name)

    def add_state(self, state):
        """Add a state to the state machine"""
        self.states[state.name] = state

    def set_state(self, state_name):
        """Change to a new state"""
        self._transition(state_name)
        self.states[self.current_state].enter(self)

    def _transition(self, state_name):
        """Perform state transition steps (exit current, update tracking)"""
        if state_name not in self.states:
            raise ValueError(f"State {state_name} does not exist")

        # Exit current state
        if self.current_state:
            self.states[self.current_state].exit(self)

        # Update state tracking
        self.previous_state = self.current_state
        self.current_state = state_name

    def push_state(self, state_name):
        """Push the current state to the stack and switch to a new state"""
        if self.current_state:
            self.state_stack.append(self.current_state)
        self.set_state(state_name)

    def pop_state(self):
        """Pop the last state from the stack and switch to it"""
        if self.state_stack:
            prev_state = self.state_stack.pop()
            self._transition(prev_state)
            self.states[self.current_state].resume(self)
        else:
            # Fallback if stack is empty - perhaps stay or go to idle?
            # For now, do nothing or maybe log warning
            pass

    def update(self):
        """Update the current state"""
        if self.current_state:
            update_start = time.monotonic()
            self.states[self.current_state].update(self)
            update_time = time.monotonic() - update_start
            if update_time > 10.0:
                logger.warning("State %s update took %.2fs", self.current_state, update_time)

    def mqtt_loop(self):
        """
        Safely call mqtt_client.loop() with error handling and gap detection.
        Catches MMQTTStateError which occurs when calling loop() on a disconnected client.
        Logs the error but continues execution—the main loop's MQTTConnectionManager
        handles reconnection on its own schedule.

        Also tracks time between mqtt_loop() calls to detect blocking operations that
        could prevent MQTT keep-alive pings from being sent.
        """
        mqtt_client = self.data.get("mqtt_client")
        if not mqtt_client:
            return

        loop_start = time.monotonic()

        try:
            mqtt_client.loop(timeout=self.mqtt_timeout)
            loop_end = time.monotonic()

            if loop_end - loop_start > self.mqtt_timeout + 1.0:
                logger.warning("MQTT loop took %.4fs", loop_end - loop_start)

            # Track gaps between mqtt_loop calls
            if self._last_loop_time is not None:
                gap = loop_start - self._last_loop_time
                # Warn if gap exceeds 60 seconds (keep-alive is 300s, so this gives buffer)
                if gap > 60.0:
                    logger.warning(
                        "MQTT loop gap: %.1fs in state '%s' (sub-state: %s)",
                        gap,
                        self.current_state,
                        self._get_substate_name(),
                    )
                # Debug log for gaps > 10 seconds
                elif gap > 10.0:
                    logger.debug(
                        "MQTT loop gap: %.1fs in state '%s'", gap, self.current_state
                    )

            self._last_loop_time = loop_start

        except Exception as e:
            # Catch MMQTTStateError and other exceptions from disconnected MQTT client
            # The string representation will contain the exception type name
            if "MMQTTStateError" in str(type(e)) or "not connected" in str(e).lower():
                # MQTT client is disconnected - log quietly and continue
                # The main loop's MQTTConnectionManager will handle reconnection
                pass
            else:
                # Other exceptions should be raised to be handled by the state machine
                raise

    def _get_substate_name(self):
        """Helper to get substate name for vent_closer composite state"""
        if self.current_state == "vent_closer" and "vent_closer" in self.states:
            vc_state = self.states["vent_closer"]
            if hasattr(vc_state, "machine"):
                return vc_state.machine.current_state
        return "N/A"

    def get_state(self):
        """Get the current state name"""
        return self.current_state

    def handle_move_request(self, vent_position):
        """
        Attempt to handle a vent move request by delegating to the current state.
        Returns True if accepted, False otherwise.
        """
        if self.current_state and self.current_state in self.states:
            return self.states[self.current_state].handle_move_request(
                self, vent_position
            )
        return False
