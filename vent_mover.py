import os
try:
    import adafruit_logging as logging
except ImportError:
    import logging
from state_machine import State
import time

logger = logging.getLogger(__name__)

class MoveVentState(State):
    def __init__(self, min_steps = 3, overshoot = 2, max_updates=3):
        super().__init__("move_vent")
        self.target_position = None
        self.move_chunk = 15 # Move in chunks to not block the loop too long
        self.min_steps = min_steps
        self.overshoot = overshoot # extra steps to counter mechanical friction and compliance
        self.max_updates = max_updates

    def handle_move_request(self, machine, target_position):
        # If we are already moving, just update the target
        self.target_position = target_position
        logger.info("MoveVentState: target updated to %.3f", self.target_position)
        return True

    def enter(self, machine):
        self.target_position = machine.data.get("target_position")
        logger.info("Moving to %.3f", self.target_position)
        self.update_counter = self.max_updates

    def update(self, machine):
        machine.mqtt_loop()
        hardware = machine.data["hardware"]
        vent = machine.data["vent"]
        
        # Update current position from hardware
        vent.update_from_hardware(hardware.read_raw_angle())
        
        # Calculate moves
        steps, direction, _, _ = vent.move_to_position(self.target_position)
        
        if steps > self.min_steps and self.update_counter > 0:
            # Move a small amount to avoid blocking main loop too long
            chunk = min(steps, self.move_chunk) + self.overshoot
            if direction: # Open (FORWARD)
                logger.info("MoveVentState: opening %d steps, counter=%d", steps, self.update_counter)
                hardware.open_vent(chunk)
            else: # Close (BACKWARD)
                logger.info("MoveVentState: closing %d steps, counter=%d", steps, self.update_counter)
                hardware.close_vent(chunk)

            # Limit the number of minor adjustment updates
            if steps < self.move_chunk:
                self.update_counter -= 1
        else:
            # Target reached
            logger.info("Target reached, popping state")
            machine.pop_state()
