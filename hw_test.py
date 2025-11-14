import time
from adafruit_motor import stepper
from state_machine import State


direction_names = {
    stepper.BACKWARD: "backward",
    stepper.FORWARD: "forward"
}


def calc_step_angle(last_angle, angle, target_step_angle, direction):
    """
    Calculate the step angle between the last angle and the current angle.
    Simplifies the zero to 360 wraparound logic.
    Assumes the encoder direction pin is pulled high.
    """
    if direction == stepper.BACKWARD:
        step_angle = angle - last_angle
        if step_angle < 180 and last_angle > 360 - target_step_angle:
            step_angle += 360
    else:
        step_angle = last_angle - angle
        if step_angle < 180 and last_angle < target_step_angle:
            step_angle += 360
    return step_angle


class TestMotion(State):
    """
    Test the motor/encoder by moving it back and forth a specified number of times.
    Non-blocking implementation that moves multiple steps per update call.
    """
    def __init__(self, moves_each_direction=5, target_step_angle=5.0, encoder_delay=0.05, pause_time=1):
        super().__init__("test_motion")
        self.moves_each_direction = moves_each_direction
        self.target_step_angle = target_step_angle
        self.direction = stepper.BACKWARD
        # State for the current move operation
        self.move_state = "idle"  # States: idle, stepping, waiting
        self.min_steps = 0
        self.current_step = 0
        self.step_angle = 0.0
        self.wait_until = 0
        self.encoder_delay = encoder_delay
        self.pause_time = pause_time

    def enter(self, machine):
        hardware = machine.data["hardware"]
        self.last_angle = hardware.read_raw_angle() * 360 / 4096
        self.num_moves = 0
        self.move_state = "idle"
        print("Entering TestMotion")

    def exit(self, machine):
        print("Exiting TestMotion")

    def update(self, machine):
        hardware = machine.data["hardware"]
        current_time = time.time()

        # State machine for the move operation
        if self.move_state == "idle":
            # Start a new move if needed
            if self.num_moves < self.moves_each_direction:
                # Initialize a new move
                self.min_steps = int(self.target_step_angle / 1.8) - 1
                self.current_step = 0
                self.step_angle = 0.0
                self.move_state = "stepping"
            else:
                # Switch direction and reset move counter
                self.direction = stepper.FORWARD if self.direction == stepper.BACKWARD else stepper.BACKWARD
                self.num_moves = 0
                print(f"Moving {self.moves_each_direction} steps of {self.target_step_angle} degrees {direction_names[self.direction]}")

        elif self.move_state == "stepping":
            while self.current_step < self.min_steps:
                # Take one step
                hardware.motor.onestep(direction=self.direction, style=stepper.DOUBLE)
                self.current_step += 1
                # TODO: Add a delay or other logic here if needed

            self.wait_until = time.time() + self.encoder_delay
            self.move_state = "waiting"

        elif self.move_state == "waiting":
            # Wait for the encoder delay
            if current_time >= self.wait_until:
                # Read the angle
                status = hardware.read_encoder_status()
                raw_angle = hardware.read_raw_angle()
                angle = raw_angle * 360 / 4096
                self.step_angle = calc_step_angle(self.last_angle, angle, self.target_step_angle, self.direction)

                # Check for bad angle reading
                if self.step_angle > self.target_step_angle + 1.8:
                    retry_raw_angle = hardware.read_raw_angle()
                    if abs(retry_raw_angle - raw_angle) > 20:
                        print(f"Bad angle reading: {angle:.2f} - raw = {raw_angle} new = {retry_raw_angle}")
                        angle = retry_raw_angle * 360 / 4096
                        self.step_angle = calc_step_angle(self.last_angle, angle, self.target_step_angle, self.direction)

                # Check if we've reached the target angle
                extra_steps = int((self.target_step_angle - self.step_angle) / 1.8)
                # if self.step_angle >= self.target_step_angle - 0.9:
                if extra_steps <= 0:
                    # Move complete
                    hardware.motor.release()
                    self.wait_until = current_time + self.encoder_delay
                    self.pre_release_angle = angle
                    self.move_state = "finishing"
                else:
                    # Take another step
                    print("Taking another", extra_steps, "steps")
                    self.move_state = "stepping"
                    self.min_steps += extra_steps
            else:
                print("Waiting...")

        elif self.move_state == "finishing":
            # Wait for the motor to settle after release
            if current_time >= self.wait_until:
                # Read final angle
                post_release_angle = hardware.read_raw_angle() * 360 / 4096
                status = hardware.read_encoder_status()
                print(f'Status={hex(status)}, Angle={post_release_angle:.2f}, Step angle = {self.step_angle:.1f}, steps={self.current_step}, backlash={post_release_angle-self.pre_release_angle:.2f}')

                # Update last angle for next move
                self.last_angle = post_release_angle
                self.num_moves += 1

                # Wait between moves
                self.wait_until = current_time + self.pause_time  # delay between moves (seconds)
                self.move_state = "delay"
            else:
                print("Finishing...")

        elif self.move_state == "delay":
            # Wait between moves
            if current_time >= self.wait_until:
                self.move_state = "idle"

