import os
try:
    import adafruit_logging as logging
except ImportError:
    import logging
from state_machine import State
import time

logger = logging.getLogger(__name__)

class MoveVentState(State):
    def __init__(self):
        super().__init__("move_vent")
        self.target_position = None
        self.move_chunk = 50 # Move in chunks to not block the loop too long

    def handle_move_request(self, machine, target_position):
        # If we are already moving, just update the target
        self.target_position = target_position
        logger.info("MoveVentState: target updated to %.3f", self.target_position)
        return True

    def enter(self, machine):
        self.target_position = machine.data.get("target_position")
        logger.info("Moving to %.3f", self.target_position)

    def update(self, machine):
        machine.mqtt_loop()
        hardware = machine.data["hardware"]
        vent = machine.data["vent"]
        
        # Update current position from hardware
        vent.update_from_hardware(hardware.read_raw_angle())
        
        # Calculate moves
        steps, direction, _, _ = vent.move_to_position(self.target_position)
        
        if steps > 0:
            # Move a small amount to avoid blocking main loop too long
            chunk = min(steps, self.move_chunk) 
            if direction: # Open (FORWARD)
                hardware.open_vent(chunk)
            else: # Close (BACKWARD)
                hardware.close_vent(chunk)
        else:
            # Target reached
            logger.info("Target reached, popping state")
            machine.pop_state()
