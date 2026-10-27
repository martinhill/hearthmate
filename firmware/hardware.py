import time
import os
import board
import busio
import neopixel
import digitalio
import analogio
from adafruit_motor import stepper
from adafruit_motorkit import MotorKit
from adafruit_bus_device.i2c_device import I2CDevice
import adafruit_logging as logging
from adafruit_itertools import count, takewhile

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Constants for encoder calculations
ENCODER_MAX_VALUE = 4096
DEGREES_PER_ENCODER_UNIT = 360.0 / ENCODER_MAX_VALUE

# I2C clock in Hz for the encoder, the motor FeatherWing and the camera; 0 keeps board.I2C()
# (100 kHz). The MLX90640 reads its EEPROM at init, which must not exceed 400 kHz.
I2C_FREQUENCY = int(os.getenv("I2C_FREQUENCY", "0"))


def calculate_encoder_delta(start_raw, end_raw):
    """
    Calculate the shortest angular distance between two encoder positions,
    handling wraparound at 0/4095.

    Args:
        start_raw: Starting encoder raw angle (0-4095)
        end_raw: Ending encoder raw angle (0-4095)

    Returns:
        Signed delta in encoder units. Positive means counterclockwise motion.
    """
    # Calculate direct difference
    delta = end_raw - start_raw

    # Handle wraparound by choosing the shortest path
    if delta > ENCODER_MAX_VALUE / 2:
        delta -= ENCODER_MAX_VALUE
    elif delta < -ENCODER_MAX_VALUE / 2:
        delta += ENCODER_MAX_VALUE

    return delta


step_params = {
    # initial delay at acceleration start
    "initial_delay": float(os.getenv("STEP_INITIAL_DELAY", "0.05")),
    # minimum delay between steps at peak speed
    "min_delay": float(os.getenv("STEP_MIN_DELAY", "0.05")),
    # final delay at deceleration end
    "final_delay": float(os.getenv("STEP_FINAL_DELAY", "0.05")),
    # delay change per step during acceleration phase
    "acceleration": float(os.getenv("STEP_ACCELERATION", "0.05")),
    # delay change per step during deceleration phase
    "deceleration": float(os.getenv("STEP_DECELERATION", "0.05")),
}


def calc_step_delays(steps, step_params):
    """Approximate a trapezoidal speed profile, suitable for movements of small step count.
    The goal is to minimize jerking and step loss.

    Args:
        steps (int): Number of steps to take.
        step_params (dict): Dictionary of step parameters.

    Returns:
        list: List of delays between each step, length steps - 1
    """

    # constrain parameters
    min_delay = max(step_params["min_delay"], 0.01)
    acceleration = max(step_params["acceleration"], 0.005)
    deceleration = max(step_params["deceleration"], 0.005)
    initial_delay = max(step_params["initial_delay"], min_delay)
    final_delay = max(step_params["final_delay"], min_delay)

    max_accel_delays = int(steps / 2)
    ramp_up = list(
        takewhile(lambda x: x > min_delay, count(initial_delay, -acceleration))
    )[:max_accel_delays]
    ramp_down = list(
        takewhile(lambda x: x > min_delay, count(final_delay, -deceleration))
    )[:max_accel_delays]
    ramp_down.reverse()

    # Take from ramp down if one too many delays
    if len(ramp_up) + len(ramp_down) >= steps:
        ramp_down = ramp_down[1:]

    top_speed = [min_delay] * (steps - len(ramp_up) - len(ramp_down) - 1)

    return ramp_up + top_speed + ramp_down


class Hardware:
    is_mock = False

    def __init__(self, i2c, step_params: dict[str, float]):
        self.i2c = i2c
        self.kit = MotorKit(i2c=self.i2c)
        self.motor = self.kit.stepper1

        self.as5600 = I2CDevice(self.i2c, 0x36)
        self.pixels = neopixel.NeoPixel(board.NEOPIXEL, 1)
        self.led = digitalio.DigitalInOut(board.LED)
        self.led.direction = digitalio.Direction.OUTPUT
        self.tmp36 = analogio.AnalogIn(board.A2)

        self.step_params = step_params
        self.position_error = None  # type: ignore # Set later by code.py or state machine

    def read_encoder_status(self):
        status_buf = bytearray(1)
        with self.as5600:
            self.as5600.write(bytes([0xB]))
            self.as5600.readinto(status_buf)
        return status_buf[0]

    def read_raw_angle(self):
        raw_angle_low = bytearray(1)
        raw_angle_high = bytearray(1)

        start = time.monotonic()
        with self.as5600:
            self.as5600.write(bytes([0xD]))
            self.as5600.readinto(raw_angle_low)
            self.as5600.write(bytes([0xC]))
            self.as5600.readinto(raw_angle_high)

        duration = time.monotonic() - start
        if duration > 1.0:
            logger.warning("AS5600 encoder read took %.2fs", duration)

        return raw_angle_high[0] << 8 | raw_angle_low[0]

    def tmp36_temperature_C(self) -> float:
        millivolts = self.tmp36.value * (self.tmp36.reference_voltage * 1000 / 65535)
        return (millivolts - 500) / 10

    def _move(self, direction, steps, settle_delay=0.05):
        # Capture starting encoder position
        start_encoder = self.read_raw_angle()

        # Calculate expected encoder change
        # Each motor step is 1.8 degrees (200 steps per revolution)
        # Encoder has 4096 values per 360 degrees
        # BACKWARD direction increases encoder value (counterclockwise)
        encoder_units_per_step = ENCODER_MAX_VALUE / 200.0
        expected_encoder_delta = steps * encoder_units_per_step
        if direction == stepper.FORWARD:
            expected_encoder_delta = -expected_encoder_delta

        # Execute the motion
        delays = calc_step_delays(steps, self.step_params)
        for delay in delays:
            self.motor.onestep(direction=direction, style=stepper.DOUBLE)
            time.sleep(delay)
        # final step
        self.motor.onestep(direction=direction, style=stepper.DOUBLE)
        time.sleep(settle_delay)

        # Measure actual encoder position after motion
        end_encoder = self.read_raw_angle()

        # Calculate actual encoder delta (handling wraparound)
        actual_encoder_delta = calculate_encoder_delta(start_encoder, end_encoder)

        # Calculate position error in degrees
        error_encoder_units = actual_encoder_delta - expected_encoder_delta
        error_degrees = error_encoder_units * DEGREES_PER_ENCODER_UNIT

        # Log the position error
        logger.debug(
            "Position error: %.2f° (steps=%d, start=%d, end=%d, expected_delta=%.1f, actual_delta=%.1f)",
            error_degrees,
            steps,
            start_encoder,
            end_encoder,
            expected_encoder_delta,
            actual_encoder_delta,
        )

        self.position_error = error_degrees

    def close_vent(self, amount=10, delay=0.05):
        self._move(stepper.BACKWARD, amount, delay)
        self.motor.release()

    def open_vent(self, amount=10, delay=0.05):
        self._move(stepper.FORWARD, amount, delay)
        self.motor.release()

    def set_pixel_color(self, color):
        self.pixels[0] = color

    def set_pixel_red(self):
        self.pixels[0] = (255, 0, 0)

    def set_pixel_green(self):
        self.pixels[0] = (0, 255, 0)

    def set_pixel_blue(self):
        self.pixels[0] = (0, 0, 255)

    def set_pixel_white(self, level=255):
        self.pixels[0] = (level, level, level)

    def set_pixel_off(self):
        self.pixels[0] = (0, 0, 0)

    def led_on(self):
        self.led.value = True

    def led_off(self):
        self.led.value = False


class MockStepper:
    """
    Mock implementation of a stepper motor that updates simulated angle when stepped.
    """

    class FakeMotor:

        def onestep(self, direction=stepper.FORWARD, style=stepper.SINGLE):
            pass

        def release(self):
            pass

    def __init__(self, parent_hardware):
        self.parent = parent_hardware
        if parent_hardware.kit is None:
            self.real_motor = self.FakeMotor()
        else:
            self.real_motor = parent_hardware.kit.stepper1
        self.style = stepper.SINGLE

    def onestep(self, direction=stepper.FORWARD, style=stepper.SINGLE):
        # Call the real motor
        self.real_motor.onestep(direction=direction, style=style)

        # Update the simulated angle
        if direction == stepper.BACKWARD:
            self.parent.current_angle = (self.parent.current_angle + 1.8) % 360
        else:
            self.parent.current_angle = (self.parent.current_angle - 1.8) % 360

        return direction

    def release(self):
        # Release the real motor
        self.real_motor.release()

        # Simulate a small backlash after release (0.2 degrees)
        if self.parent.last_direction == stepper.BACKWARD:
            self.parent.current_angle = (self.parent.current_angle - 0.2) % 360
        else:
            self.parent.current_angle = (self.parent.current_angle + 0.2) % 360


class MockHardware(Hardware):
    """
    Mock implementation of Hardware class that simulates encoder values
    based on motor steps. Each step is assumed to be 1.8 degrees.
    """

    is_mock = True

    def __init__(self, i2c, step_params: dict[str, float]):
        self.i2c = i2c
        try:
            self.kit = MotorKit(i2c=self.i2c)
        except Exception as e:
            self.kit = None

        # Simulated encoder state
        self.current_angle: float = 0.0  # Current angle in degrees
        self.last_direction = stepper.FORWARD
        self.open_position: int = int(os.getenv("OPEN_POSITION"))

        # Create a mock motor that updates the angle when stepped
        self.motor = MockStepper(self)

        # Use real Neopixel and LED
        self.pixels = neopixel.NeoPixel(board.NEOPIXEL, 1)
        self.led = digitalio.DigitalInOut(board.LED)
        self.led.direction = digitalio.Direction.OUTPUT
        self.tmp36 = analogio.AnalogIn(board.A2)

        self.step_params = step_params
        self.position_error = None  # type: ignore # Set later by code.py or state machine

    def read_encoder_status(self):
        # Return a simulated "good" status
        return 0x47  # Arbitrary status value indicating no magnet detected

    def read_raw_angle(self):
        # Convert current angle to raw encoder value (4096 values for 360 degrees)
        raw_value = int((self.current_angle % 360) * 4096 / 360) + self.open_position
        return raw_value

    def tmp36_temperature_C(self) -> float:
        tmp36 = super().tmp36_temperature_C()
        return max(tmp36, 0)

    def mock_move_to_angle(self, angle):
        self.current_angle = angle

    def mock_move_to_raw_angle(self, raw_angle):
        self.current_angle = (
            (raw_angle - self.open_position + 4096) * 360 / 4096
        ) % 360


def get_hardware() -> Hardware:
    """
    Get the hardware object based on the presence of the AS5600 encoder.
    """
    if I2C_FREQUENCY:
        i2c = busio.I2C(board.SCL, board.SDA, frequency=I2C_FREQUENCY)
    else:
        i2c = board.I2C()
    i2c.try_lock()
    scan = i2c.scan()
    i2c.unlock()
    logger.info(
        "I2C scan at %d Hz: %s", I2C_FREQUENCY or 100000, [hex(addr) for addr in scan]
    )

    if 0x36 in scan and 0x60 in scan:
        hardware = Hardware(i2c, step_params)
    else:
        logger.info("AS5600 not found - using mock hardware")
        hardware = MockHardware(i2c, step_params)
    return hardware
