import time

try:
    import adafruit_logging as logging
except ImportError:
    import logging

from state_machine import State, StateMachine

logger = logging.getLogger(__name__)


class VentFunctionABC():
    """Abstract base class for an adjustable air vent position vs time function. Calculates the desired
    position of the air vent, from 0.0 to 1.0, at the current time.

    To allow for manual overriding of the computed trajectory, the function may be adjusted to a new
    position, resulting in a change of the end time to reach the closed position such that the new
    computed trajectory continues at the same rate.
    """

    def __init__(self, time_range=30*60, time_func=time.time):
        self.time_range = time_range
        self.time_func = time_func
        self.start_time = None
        self.time_adjustment = 0

    def start(self, vent_current_position):
        self.start_time = self.time_func()
        self.start_position = vent_current_position
        self.time_adjustment = 0

    def get_position(self):
        "Calculates the position at the current (adjusted) time"
        raise NotImplementedError

    def inverse(self, position) -> float:
        "Returns the time for given position in range (0, time_range)"
        raise NotImplementedError

    def get_elapsed_time(self):
        "Get time since start() was called, un-adjusted"
        if self.start_time is None:
            return 0
        return self.time_func() - self.start_time

    def get_adjusted_time(self):
        "Get adjusted elapsed time since start in range of 0 to time_range"
        if self.start_time is None:
            return 0
        elapsed_time = self.time_func() + self.time_adjustment - self.start_time
        # logger.debug("Function elapsed_time=%d", elapsed_time)
        return elapsed_time

    def adjust(self, new_position):
        new_position = max(0.0, min(new_position,1.0))
        self.time_adjustment = self.inverse(new_position) - self.time_func() + self.start_time
        logger.info("Function adjust: time_adjustment=%d", self.time_adjustment)


class LinearVentFunction(VentFunctionABC):
    """Just a simple straight line.
    """

    def inverse(self, position):
        pct_moved = (position - self.start_position) / (1.0 - self.start_position)
        return int(self.time_range * pct_moved)

    def get_position(self):
        pct_time = self.get_adjusted_time() / self.time_range
        target_pos = self.start_position + (1.0 - self.start_position) * pct_time
        target_pos = max(0.0, min(target_pos, 1.0))
        return target_pos


class Monitoring(State):

    def __init__(self, min_steps = 8, override_sensitivity = 0.01):
        super().__init__("monitoring")
        self.min_steps = min_steps
        self.override_sensitivity = override_sensitivity

    def enter(self, machine):
        "Record the initial vent position for override detection"
        hardware = machine.data["hardware"]
        vent = machine.data["vent"]
        vent.update_from_hardware(hardware.read_raw_angle())
        self.vent_position = vent.get_position()
        logger.info("Monitoring: vent_position=%.3f", self.vent_position)
        hardware.set_pixel_blue()

    def resume(self, machine):
        "Update vent_position to prevent erroneous override detection"
        hardware = machine.data["hardware"]
        vent = machine.data["vent"]
        vent.update_from_hardware(hardware.read_raw_angle())
        self.vent_position = vent.get_position()

    def update(self, machine):
        hardware = machine.data["hardware"]
        vent = machine.data["vent"]
        func = machine.data["function"]
        machine.mqtt_loop()
        vent.update_from_hardware(hardware.read_raw_angle())

        # Override detection
        displacement = vent.get_position() - self.vent_position
        if abs(displacement) > self.override_sensitivity:
            logger.debug("monitoring: displacement=%.3f", displacement)
            machine.set_state("override")
        else:
            # Should initiate motion?
            ideal_position = func.get_position()
            steps = vent.move_to_position(ideal_position)[0]
            logger.debug("ideal_position=%.4f, steps=%d, displacement=%.3f", ideal_position, steps, displacement)
            if steps >= self.min_steps or ideal_position > 0.999:
                # Initiate motion
                machine.set_state("closing")

    def handle_move_request(self, machine, target_position):
        "Always allow manual move requests while monitoring"
        return True

class Override(State):
    """Wait for externally-actuated motion to stop.
    """

    def __init__(self, open_position_threshold = 0.1, sensitivity = 0.005, settle_time = 3.0):
        super().__init__("override")
        self.open_position_threshold = open_position_threshold
        self.sensitivity = sensitivity
        self.settle_time = settle_time

    def enter(self, machine):
        logger.info("Detected manual override")
        hardware = machine.data["hardware"]
        vent = machine.data["vent"]
        vent.update_from_hardware(hardware.read_raw_angle())
        self.vent_position = vent.get_position()
        hardware.set_pixel_green()
        self.last_check_time = time.time()
        logger.info("Override: vent_position=%.3f", self.vent_position)

    def update(self, machine):
        hardware = machine.data["hardware"]
        vent = machine.data["vent"]

        # Override detection
        vent.update_from_hardware(hardware.read_raw_angle())
        position = vent.get_position()
        displacement = position - self.vent_position
        if abs(displacement) > self.sensitivity:
            # Movement observed - update vent position and indicate vent is open if it is
            self.last_check_time = time.time()
            self.vent_position = position
            if position < self.open_position_threshold:
                # Indicate vent is open
                hardware.set_pixel_color((0, 64, 64))  # teal
        elif time.time() - self.last_check_time > self.settle_time:
            # No movement ovserved for the settling time
            func = machine.data["function"]
            if machine.data["vent_closed"] and position < self.open_position_threshold:
                # Vent moved open - reset function to start
                func.start(position)
                machine.data["vent_closed"] = False
                logger.info("Override: vent moved open - reset function to start")
            else:
                func.adjust(position)
                logger.info("Override: adjusted function to position %.3f", position)
            machine.set_state("monitoring")


class Closing(State):
    """Close the air vent by the amount needed.
    """

    def __init__(self, min_steps = 5, overshoot = 2, closed_threshold = 0.999):
        super().__init__("closing")
        self.min_steps = min_steps
        self.overshoot = overshoot # extra steps to counter mechanical friction and compliance
        self.closed_threshold = closed_threshold

    def enter(self, machine):
        hardware = machine.data["hardware"]
        hardware.set_pixel_red()
        func = machine.data["function"]
        self.ideal_position = func.get_position()
        logger.info("Closing to position %.3f", self.ideal_position)

    def update(self, machine):
        hardware = machine.data["hardware"]
        vent = machine.data["vent"]
        if self.ideal_position < self.closed_threshold:
            vent.update_from_hardware(hardware.read_raw_angle())
            steps, direction, encoder, revs = vent.move_to_position(self.ideal_position)
            logger.debug("ideal_position=%.3f, steps=%d, direction=%d", self.ideal_position, steps, direction)
            if steps > self.min_steps:
                if direction:
                    hardware.open_vent(steps + self.overshoot)
                else:
                    hardware.close_vent(steps + self.overshoot)
            else:
                machine.set_state("monitoring")
        else:
            machine.set_state("closed")


class Closed(State):
    """Final state reached after air vent is fully closed. On entry slams vent closed, 
    then waits until the air vent is moved to full open and resets function.
    """

    def __init__(self, open_position_threshold = 0.1, extra_steps = 2, sensitivity = 0.001):
        super().__init__("closed")
        self.open_position_threshold = open_position_threshold
        self.extra_steps = extra_steps
        self.sensitivity = sensitivity

    def enter(self, machine):
        hardware = machine.data["hardware"]
        vent = machine.data["vent"]
        hardware.set_pixel_white()
        machine.mqtt_loop()
        vent.update_from_hardware(hardware.read_raw_angle())
        steps, direction, encoder, revs = vent.move_to_position(1.0)
        # brute force
        hardware.close_vent(steps + self.extra_steps)
        time.sleep(0.1)
        vent.update_from_hardware(hardware.read_raw_angle())
        self.last_position = vent.get_position()
        machine.data["vent_closed"] = True
        logger.info("Closed")

    def update(self, machine):
        hardware = machine.data["hardware"]
        vent = machine.data["vent"]

        vent.update_from_hardware(hardware.read_raw_angle())
        vent_position = vent.get_position()
        # Check MQTT when vent position is stable
        if abs(vent_position - self.last_position) < self.sensitivity:
            machine.mqtt_loop()
        if vent_position < self.last_position - 0.01:
            # Vent is moving open
            machine.set_state("override")
        if vent_position < self.open_position_threshold:
            func = machine.data["function"]
            func.start(vent_position)
            machine.set_state("monitoring")

    def handle_move_request(self, machine, target_position):
        "Always allow manual move requests while closed"
        return True

    def resume(self, machine):
        "Do nothing..."
        pass


class VentCloser(State):
    """
    Closes the air vent gradually according to a provided function.
    """

    def __init__(self, function):
        super().__init__("vent_closer")
        self.machine = StateMachine()
        self.machine.add_state(Monitoring())
        self.machine.add_state(Override())
        self.machine.add_state(Closing())
        self.machine.add_state(Closed())
        self.machine.data["function"] = function
        self.machine.data["vent_closed"] = False

    def handle_move_request(self, machine, target_position):
        # Deligate to sub-states
        if self.machine.handle_move_request(target_position):
            # Allow interruption
            machine.data["target_position"] = target_position
            machine.push_state("move_vent")
            return True
        return False

    def enter(self, machine):
        for key in "vent", "hardware", "mqtt_client":
            self.machine.data[key] = machine.data[key]
        hardware = machine.data["hardware"]
        vent = machine.data['vent']
        vent.update_from_hardware(hardware.read_raw_angle())
        function = self.machine.data["function"]
        
        # Normal start
        function.start(vent.get_position())

        self.machine.set_state("monitoring")
        logger.info("%s entered", self.name)

    def resume(self, machine):
        "Resume current sub-state after adjusting function for new position"
        hardware = machine.data["hardware"]
        vent = machine.data['vent']
        vent.update_from_hardware(hardware.read_raw_angle())
        function = self.machine.data["function"]
        
        # Resuming logic: Adjust function to current position
        function.adjust(vent.get_position())
        self.machine.states[self.machine.current_state].resume(self.machine)
        logger.info("%s resumed", self.name)

    def exit(self, machine):
        logger.info("exit %s", self.name)

    def update(self, machine):
        self.machine.update()