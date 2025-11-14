import os

# Constants for the AS5600 encoder
ENCODER_MAX_VALUE = 4096 # 12-bit encoder
ENCODER_HALF_MAX_VALUE = ENCODER_MAX_VALUE / 2
ENCODER_QUADRANT_SIZE = ENCODER_HALF_MAX_VALUE / 4
ENCODER_FIRST_QUADRANT_START = 0
ENCODER_FIRST_QUADRANT_END = ENCODER_QUADRANT_SIZE
ENCODER_SECOND_QUADRANT_START = ENCODER_QUADRANT_SIZE
ENCODER_SECOND_QUADRANT_END = ENCODER_HALF_MAX_VALUE
ENCODER_THIRD_QUADRANT_START = ENCODER_HALF_MAX_VALUE
ENCODER_THIRD_QUADRANT_END = ENCODER_MAX_VALUE - ENCODER_QUADRANT_SIZE
ENCODER_FOURTH_QUADRANT_START = ENCODER_MAX_VALUE - ENCODER_QUADRANT_SIZE
ENCODER_FOURTH_QUADRANT_END = ENCODER_MAX_VALUE

# Number of motor steps per revolution of the motor
MOTOR_STEPS_PER_REVOLUTION = os.getenv("MOTOR_STEPS_PER_REVOLUTION", 200)

class Damper:
    """
    Damper represents the state of the damper as a percentage (0.0 to 1.0) of the way closed,
    inferred from the encoder raw angle position. 0.0 is fully open, 1.0 is fully closed.
    """

    ## Calibrated values

    # The AS5600 encoder raw angle position when the damper is fully open
    # A value of None indicates that the position has not been calibrated yet
    # Note: Encoder raw angle value increases with counterclockwise rotation (closing the damper)
    open_position = None
    # The AS5600 encoder raw angle position when the damper is fully closed,
    # with the number of zero crossings taken into account
    closed_position = None
    
    # Number of zero crossings of the encoder raw angle position between fully open and fully closed,
    # reflecting the gear ratio of the motor and the damper, and the damper's physical range of motion.
    num_zero_crossings = 0 

    ## Current state

    # Current revolution of the encoder. Incremented when the encoder raw angle position crosses 0.
    # A value of 0 indicates that the damper is at the fully open position if the encoder is reading open_position.
    current_revolution = 0

    # Last encoder raw angle position read from the hardware
    last_angle = None

    def __init__(self, open_position=None, closed_position=None, num_zero_crossings=0):
        self.open_position = open_position
        self.closed_position = closed_position + ENCODER_MAX_VALUE * num_zero_crossings
        self.num_zero_crossings = num_zero_crossings
        self.current_revolution = 0  # Initialize to 0 instead of None
        self.last_angle = None  # Initialize in __init__ instead of class-level

    def update_from_hardware(self, current_angle):
        """
        Update the current state (current_revolution and last_angle) from the encoder raw angle position.
        """
        # Initialize last_angle on first call
        if self.last_angle is None:
            self.last_angle = current_angle
            return
            
        # Check if the encoder raw angle position has crossed 0
        if current_angle < ENCODER_FIRST_QUADRANT_END and self.last_angle > ENCODER_FOURTH_QUADRANT_START:
            # Crossing from fourth quadrant to first quadrant (counterclockwise)
            self.current_revolution += 1
        elif current_angle > ENCODER_FOURTH_QUADRANT_START and self.last_angle < ENCODER_FIRST_QUADRANT_END:
            # Crossing from first quadrant to fourth quadrant (clockwise)
            self.current_revolution -= 1
        self.last_angle = current_angle

    def get_position(self, current_angle):
        """
        Get the current position of the damper as a percentage of the way closed.
        Assumes that the encoder raw angle position has already been updated from the hardware
        by calling update_from_hardware().
        Requires that open_position and closed_position have been set (calibrated).
        """
        # Assumes calibration has occurred
        open_pos = self.open_position if self.open_position is not None else 0
        closed_pos = self.closed_position if self.closed_position is not None else ENCODER_MAX_VALUE
        
        # Convert the encoder raw angle position to a normalized position accounting for the current revolution
        normalized_position = current_angle + self.current_revolution * ENCODER_MAX_VALUE - open_pos
        # Convert the normalized position to a percentage of the way closed
        return normalized_position / (closed_pos - open_pos)

    def open(self, amount=0.1):
        """
        Calculate the number of motor steps and target angle to open the damper by the specified amount (0.0 to 1.0).
        Returns (num_steps, target_angle, revolutions)
        Assumes that the encoder raw angle position has already been updated from the hardware
        by calling update_from_hardware().
        """
        # Opening decreases the position percentage
        current_position = self.get_position(self.last_angle)
        target_position = max(0.0, current_position - amount)
        num_steps, direction, target_angle, revolutions = self.move_to_position(target_position)
        # For open(), direction should be FORWARD, return simplified tuple
        return (num_steps, target_angle, revolutions)

    def close(self, amount=0.1):
        """
        Calculate the number of motor steps and target angle to close the damper by the specified amount (0.0 to 1.0).
        Returns (num_steps, target_angle, revolutions)
        Assumes that the encoder raw angle position has already been updated from the hardware
        by calling update_from_hardware().
        """
        # Closing increases the position percentage
        current_position = self.get_position(self.last_angle)
        target_position = min(1.0, current_position + amount)
        num_steps, direction, target_angle, revolutions = self.move_to_position(target_position)
        # For close(), direction should be BACKWARD, return simplified tuple
        return (num_steps, target_angle, revolutions)

    def move_to_position(self, position=0.5):
        """
        Calculate the number of motor steps and target angle to move the damper to the specified position (0.0 to 1.0).
        Returns (num_steps, direction, target_angle, revolutions)
        Assumes that the encoder raw angle position has already been updated from the hardware
        by calling update_from_hardware().
        Requires that open_position and closed_position have been set (calibrated).
        """
        # Clamp position to valid range
        position = max(0.0, min(1.0, position))

        # Assumes calibration has occurred
        open_pos = self.open_position if self.open_position is not None else 0
        closed_pos = self.closed_position if self.closed_position is not None else ENCODER_MAX_VALUE
        last_angle = self.last_angle if self.last_angle is not None else 0

        # Calculate the target encoder angle, unnbounded by the encoder max value
        target_position_angle = int(round(open_pos + position * (closed_pos - open_pos)))

        # Calculate the current encoder angle accounting for the current revolution
        current_position_angle = last_angle + self.current_revolution * ENCODER_MAX_VALUE

        # Calculate the difference in encoder angles, accounting for the current revolution
        angle_difference = target_position_angle - current_position_angle

        # Determine direction: positive angle_difference means we need to go counterclockwise (close, BACKWARD)
        if angle_difference < 0:
            direction = 1  # BACKWARD (closing)
            encoder_delta = -angle_difference
        else:
            direction = 0  # FORWARD (opening)
            encoder_delta = angle_difference

        # Convert encoder delta to motor steps
        # The relationship depends on the gear ratio between motor and encoder
        # Motor has 200 steps per revolution, encoder has 4096 values per revolution
        # So 200 motor steps = 4096 encoder values
        motor_steps = int(round(encoder_delta * MOTOR_STEPS_PER_REVOLUTION / ENCODER_MAX_VALUE))
        
        # Calculate target revolution number
        revolutions = int(target_position_angle / ENCODER_MAX_VALUE)

        # Calculate the target encoder angle accounting for the current and target revolutions
        target_encoder_angle = target_position_angle % ENCODER_MAX_VALUE

        return (motor_steps, direction, target_encoder_angle, revolutions)


def create_damper_from_env():
    """
    Create a Damper object from the environment variables.
    """
    open_position = int(os.getenv("OPEN_POSITION"))
    closed_position = int(os.getenv("CLOSED_POSITION"))
    num_zero_crossings = int(os.getenv("NUM_ZERO_CROSSINGS"))
    return Damper(open_position, closed_position, num_zero_crossings)