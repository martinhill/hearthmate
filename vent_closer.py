import time

import adafruit_logging as logging

from state_machine import State, StateMachine

logger = logging.getLogger(__name__)


class Monitoring(State):

    def __init__(self):
        super().__init__("monitoring")

    def update(self, machine):
        hardware = machine.data["hardware"]
        vent = machine.data["vent"]
        vent.update_from_hardware(hardware.read_raw_angle())
        machine.data["mqtt_client"].loop()


class Override(State):
    """Wait for externally-actuated motion to stop.
    """

    def __init__(self):
        super().__init__("override")

    def enter(self, machine):
        logger.info("Detected manual override")
        hardware = machine.data["hardware"]
        hardware.set_pixel_green()

    def update(self, machine):
        machine.data["mqtt_client"].loop()


class Closing(State):
    """Close the air vent by the amount needed.
    """

    def __init__(self):
        super().__init__("closing")

    def enter(self, machine):
        hardware = machine.data["hardware"]
        hardware.set_pixel_red()

    def update(self, machine):
        pass


class Closed(State):
    """Final state reached after air vent is fully closed
    """

    def __init__(self):
        super().__init__("closed")

    def update(self, machine):
        machine.data["mqtt_client"].loop()


class VentCloser(State):
    """
    Closes the air vent gradually in a linear distance/time relation. 
    """

    def __init__(self, time_seconds: int):
        super().__init__("linear_time_close")
        self.time_seconds = time_seconds
        self.machine = StateMachine()
        self.machine.add_state(Monitoring())
        self.machine.add_state(Override())
        self.machine.add_state(Closing())
        self.machine.add_state(Closed())

    def enter(self, machine):
        for key in "vent", "hardware", "mqtt_client":
            self.machine.data[key] = machine.data[key]
        hardware = machine.data["hardware"]
        vent = machine.data['vent']
        vent.update_from_hardware(hardware.read_raw_angle())


        self.machine.set_state("monitoring")
        logger.info("Closing vent entered")

    def exit(self, machine):
        logger.info("exit %s", self.name)

    def update(self, machine):
        self.machine.update()