import time
import board
import neopixel
import digitalio
from adafruit_motor import stepper
from adafruit_motorkit import MotorKit
from adafruit_bus_device.i2c_device import I2CDevice
import adafruit_logging as logging

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


class Hardware:
    
    def __init__(self, i2c):
        self.i2c = i2c
        self.kit = MotorKit(i2c=self.i2c)
        self.motor = self.kit.stepper1

        self.as5600 = I2CDevice(self.i2c, 0x36)
        self.pixels = neopixel.NeoPixel(board.NEOPIXEL, 1)
        self.led = digitalio.DigitalInOut(board.LED)
        self.led.direction = digitalio.Direction.OUTPUT

    def read_encoder_status(self):
        status_buf = bytearray(1)
        with self.as5600:
            self.as5600.write(bytes([0xB]))
            self.as5600.readinto(status_buf)
        return status_buf[0]

    def read_raw_angle(self):
        raw_angle_low = bytearray(1)
        raw_angle_high = bytearray(1)

        with self.as5600:
            self.as5600.write(bytes([0xD]))
            self.as5600.readinto(raw_angle_low)
            self.as5600.write(bytes([0xC]))
            self.as5600.readinto(raw_angle_high)

        return raw_angle_high[0] << 8 | raw_angle_low[0]

    def _move(self, direction, steps, delay=0.05):
        for i in range(steps):
            self.motor.onestep(direction=direction, style=stepper.DOUBLE)
            time.sleep(delay)

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

    def set_pixel_white(self):
        self.pixels[0] = (255, 255, 255)

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
    def __init__(self, parent_hardware):
        self.parent = parent_hardware
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
    def __init__(self, i2c):
        self.i2c = i2c
        self.kit = MotorKit(i2c=self.i2c)

        # Simulated encoder state
        self.current_angle = 0.0  # Current angle in degrees
        self.last_direction = stepper.FORWARD

        # Create a mock motor that updates the angle when stepped
        self.motor = MockStepper(self)

    def read_encoder_status(self):
        # Return a simulated "good" status
        return 0x20  # Arbitrary status value indicating normal operation

    def read_raw_angle(self):
        # Convert current angle to raw encoder value (4096 values for 360 degrees)
        raw_value = int((self.current_angle % 360) * 4096 / 360)
        return raw_value


def get_hardware():
    """
    Get the hardware object based on the presence of the AS5600 encoder.
    """
    i2c = board.I2C()
    i2c.try_lock()
    scan = i2c.scan()
    i2c.unlock()
    logger.info("I2C scan: %s", [hex(addr) for addr in scan])

    if 0x36 in scan:
        hardware = Hardware(i2c)
    else:
        logger.info("AS5600 not found - using mock hardware")
        hardware = MockHardware(i2c)
    return hardware
