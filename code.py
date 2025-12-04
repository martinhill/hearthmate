import time
import os
import wifi
import socketpool
import ssl

from adafruit_minimqtt.adafruit_minimqtt import MQTT, MMQTTException, MMQTTStateError
import adafruit_logging as logging

from state_machine import StateMachine, State
from hw_test import TestMotion, logger as test_logger
from hardware import get_hardware, logger as hw_logger
from airvent import create_vent_from_env
from vent_closer import VentCloser, LinearVentFunction, logger as vent_logger
from vent_mover import MoveVentState, logger as vm_logger
from logging import MQTTHandler, FileHandler
from connections import (
    WiFiConnectionManager,
    MQTTConnectionManager,
    I2CDeviceRecoveryManager,
    logger as conn_logger,
)
from homeassistant import HomeAssistant, logger as ha_logger
from thermal_camera import get_thermal_camera, logger as cam_logger
from stovelink import StoveLinkEncoder, logger as stovelink_logger

VENT_CLOSE_TIME = os.getenv("VENT_CLOSE_TIME", 60 * 60)
THERMAL_CAMERA_INTERVAL = int(os.getenv("THERMAL_CAMERA_INTERVAL", 30))  # seconds
THERMAL_MAX_TEMP_CHANGE = float(os.getenv("THERMAL_MAX_TEMP_CHANGE", 30.0))  # degrees C
MEASUREMENT_BUFFER_INTERVAL = int(
    os.getenv("MEASUREMENT_BUFFER_INTERVAL", 15)
)  # seconds

logger = logging.getLogger(__name__)
log_level = getattr(logging, os.getenv("LOG_LEVEL", "INFO"), logging.INFO)
file_log_level = getattr(logging, os.getenv("FILE_LOG_LEVEL", "INFO"), logging.INFO)
logger.setLevel(log_level)
stream_handler = logging.StreamHandler()
logger.addHandler(stream_handler)
mqtt_logger = logging.getLogger("mqtt")


class IdleState(State):
    """
    Idle state.
    If vent movement to fully closed position is detected, and then fully open,
    the state machine will transition to "vent_closer" state.
    """

    def __init__(self, sensitivity=0.001):
        super().__init__("idle")
        self.detected_fully_closed = False
        self.sensitivity = sensitivity

    def enter(self, machine):
        hardware = machine.data["hardware"]
        hardware.motor.release()
        hardware.set_pixel_color((0, 32, 32))  # teal
        vent.update_from_hardware(hardware.read_raw_angle())
        self.last_position = vent.get_position()
        logger.info("Idle")

    def resume(self, machine):
        hardware = machine.data["hardware"]
        hardware.motor.release()
        if self.detected_fully_closed:
            hardware.set_pixel_color((0x8F, 0x8F, 0))  # yellow
        logger.info("resumed Idle")

    def handle_move_request(self, machine, target_position):
        machine.data["target_position"] = target_position
        machine.push_state("move_vent")
        return True

    def update(self, machine):
        hardware = machine.data["hardware"]
        vent = machine.data["vent"]
        vent.update_from_hardware(hardware.read_raw_angle())
        vent_position = vent.get_position()
        # Check MQTT when vent position is stable
        if abs(vent_position - self.last_position) < self.sensitivity:
            machine.mqtt_loop()
        self.last_position = vent_position
        if vent_position > 0.99 and not self.detected_fully_closed:
            # first detection of vent closed
            self.detected_fully_closed = True
            hardware.set_pixel_color((0x8F, 0x8F, 0))  # yellow
            logger.info("Detected vent closed")
        elif vent_position < 0.01 and self.detected_fully_closed:
            # detected vent fully open after closed position - transition
            logger.info("Detected vent open - engaging automatic vent closer")
            machine.set_state("vent_closer")


class Calibrate(State):
    """
    Calibrate the motor/encoder by observing the encoder position when moving it back and forth a specified number of times.
    The motion should be first to fully open the air vent, then to fully close it.
    Assumes the encoder direction pin is pulled high.
    """

    def __init__(self):
        super().__init__("calibrate_high")

    def enter(self, machine):
        hardware = machine.data["hardware"]
        hardware.motor.release()

    def update(self, machine):
        hardware = machine.data["hardware"]


mqtt_topic = os.getenv("MQTT_TOPIC")
command_topic = mqtt_topic + "/command"


def init_mqtt_client(
    message_callback,
    mqtt_host=os.getenv("MQTT_HOST"),
    mqtt_user=os.getenv("MQTT_USER"),
    mqtt_password=os.getenv("MQTT_PASSWORD"),
) -> MQTT:
    # Create a socket pool
    pool = socketpool.SocketPool(wifi.radio)
    ssl_context = ssl.create_default_context()
    mqtt_client = MQTT(
        broker=mqtt_host,
        port=1883,
        username=mqtt_user,
        password=mqtt_password,
        socket_pool=pool,
        ssl_context=ssl_context,
    )

    def connected(client, userdata, flags, rc):
        # This function will be called when the client is connected
        # successfully to the broker.
        logger.info(
            "Connected to %s! Listening for commands on %s", mqtt_host, command_topic
        )
        # Subscribe to all changes on the onoff_feed.
        client.subscribe(command_topic)

    def disconnected(client, userdata, rc):
        # This method is called when the client is disconnected
        logger.warning("Disconnected from %s!", mqtt_host)

    # Setup the callback methods above
    mqtt_client.on_connect = connected
    mqtt_client.on_disconnect = disconnected
    mqtt_client.on_message = message_callback
    mqtt_client.logger = mqtt_logger

    return mqtt_client


def init_state_machine(mqtt_client, hardware, vent, closer_function):
    # Create the state machine
    machine = StateMachine()
    machine.data["hardware"] = hardware
    machine.data["mqtt_client"] = mqtt_client
    machine.data["vent"] = vent
    machine.add_state(TestMotion(moves_each_direction=2, target_step_angle=30.0))
    machine.add_state(IdleState())
    machine.add_state(MoveVentState())
    machine.add_state(VentCloser(closer_function))
    return machine


def check_encoder(hardware):
    encoder_status = hardware.read_encoder_status()
    encoder_md = bool(encoder_status & 0x20)
    encoder_ml = bool(encoder_status & 0x10)
    encoder_mh = bool(encoder_status & 0x8)
    if encoder_md and not encoder_mh and not encoder_ml:
        logger.info("Encoder status ok: magnet detected (STATUS=0x%X)", encoder_status)
    elif encoder_md and (encoder_ml or encoder_mh):
        magnet_condition = "weak" if encoder_ml else "strong"
        logger.warning(
            "Encoder status check: magnet detected but too %s (STATUS=0x%X)",
            magnet_condition,
            encoder_status,
        )
    else:
        logger.error("Encoder status check failed: STATUS=0x%X", encoder_status)
    return (encoder_md, encoder_ml, encoder_mh)


def setup_loggers(mqtt_client: MQTT, mqtt_handler: MQTTHandler):
    timestamp_formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    mqtt_handler.setFormatter(timestamp_formatter)
    file_handler = FileHandler("logs")
    file_handler.setFormatter(timestamp_formatter)
    file_handler.setLevel(file_log_level)
    logger_handlers_mapping = {
        logger: [mqtt_handler, file_handler],
        hw_logger: [mqtt_handler, file_handler],
        test_logger: [mqtt_handler, file_handler],
        vent_logger: [mqtt_handler, file_handler],
        conn_logger: [file_handler, stream_handler],
        mqtt_logger: [file_handler, stream_handler],
        ha_logger: [mqtt_handler, stream_handler],
        vm_logger: [mqtt_handler, file_handler],
        cam_logger: [mqtt_handler, file_handler],
        stovelink_logger: [mqtt_handler, file_handler],
    }
    for lgr, handlers in logger_handlers_mapping.items():
        for handler in handlers:
            lgr.addHandler(handler)
        lgr.setLevel(log_level)
    mqtt_logger.setLevel(
        getattr(logging, os.getenv("MQTT_LOG_LEVEL", "INFO"), logging.INFO)
    )


def get_combustion_time(closer_function: VentFunctionABC) -> int:
    """
    Get elapsed combustion time in seconds.

    Args:
        machine: State machine instance

    Returns:
        int: Seconds since burn cycle started (0 if not in vent_closer state)
    """
    elapsed = closer_function.get_elapsed_time()
    return int(max(0, elapsed))
    return 0

def do_thermal_camera_stuff(camera_exception_raised):
    """
    Do all the thermal camera stuff here to keep the main loop clean.
    Returns True if successful, False if camera recovery is needed.
    """
    global thermal_camera, ha, mqtt_client, vent, stovelink_encoder, machine, mqtt_topic

    if camera_exception_raised:
        recovered_camera = thermal_recovery.attempt_recovery(current_time)
        if recovered_camera:
            thermal_camera = recovered_camera
            logger.info("Thermal camera recovered and operational")
            ha.update_camera_ok(True)
            # Successful capture - reset error count
            thermal_recovery.reset_error_count()
            camera_exception_raised = False
        else:
            return False

    camera_start_time = time.monotonic()
    frame = thermal_camera.capture_frame()
    if frame:
        # Calculate temperature statistics
        stats_start_time = time.monotonic()
        np_frame = thermal_camera.get_np_frame()
        stats = thermal_camera.get_temperature_statistics(np_frame)

        # Validate stats to filter out erroneous readings
        if ha.validate_thermal_stats(stats, THERMAL_MAX_TEMP_CHANGE):
            # Stats are valid - publish image and statistics

            # Encode and publish StoveLink binary packet (use numpy frame for efficiency)
            stovelink_start = time.monotonic()
            vent_position = vent.get_position()
            combustion_time = get_combustion_time(closer_function)
            stovelink_packet = stovelink_encoder.encode_packet(
                np_frame, vent_position, combustion_time
            )
            try:
                mqtt_client.publish(
                    mqtt_topic + "/stovelink", stovelink_packet
                )

                camera_end_time = time.monotonic()
                stovelink_time = camera_end_time - stovelink_start
                camera_process_time = camera_end_time - camera_start_time
                capture_time = stats_start_time - camera_start_time
                stats_calc_time = stovelink_start - stats_start_time
                logger.debug(
                    "Stats: %.1f %.1f %.1f %.1f, time=%.4fs (cap=%.4fs, stat=%.4fs, sl=%.4fs)",
                    stats["min"],
                    stats["max"],
                    stats["mean"],
                    stats["median"],
                    camera_process_time,
                    capture_time,
                    stats_calc_time,
                    stovelink_time,
                )
                ha.update_thermal_statistics(stats)
            except MMQTTStateError as e:
                logger.warning("MMQTTStateError during StoveLink MQTT packet publication: %s", e)
            except OSError as e:
                # Wrap OSError in MMQTTException to be handled by the main loop
                # Otherwise it would trigger camera recovery manager
                raise MMQTTException(f"OSError during StoveLink MQTT packet publication: {e}")
    return True


if __name__ == "__main__":

    def command_handler(message):
        global machine
        if message == "test":
            machine.set_state("test_motion")
        elif message == "close" or message == "vent_closer":
            machine.set_state("vent_closer")
        elif message == "stop" or message == "idle":
            machine.set_state("idle")
        else:
            logger.warning("Unknown command: %s", message)

    # Map topic to callable
    mqtt_message_handlers = {command_topic: command_handler}

    def message_callback(client, topic, message):
        # This method is called when a topic the client is subscribed to
        # has a new message.
        # logger.debug("topic %s received %s", topic, message)
        if topic in mqtt_message_handlers:
            handler = mqtt_message_handlers[topic]
            # logger.debug("calling %s", handler)
            handler(message)
        else:
            logger.warning("Unhandled message on topic %s: %s", topic, message)

    # Initialize connection managers
    wifi_manager = WiFiConnectionManager()
    mqtt_client: MQTT = init_mqtt_client(message_callback)
    mqtt_conn_manager = MQTTConnectionManager(mqtt_client, wifi_manager)

    # Set up MQTT and file logging handlers
    mqtt_handler = MQTTHandler(mqtt_client, mqtt_topic + "/log")
    setup_loggers(mqtt_client, mqtt_handler)

    # Get the hardware interface
    hardware = get_hardware()
    hardware.led_on()
    vent = create_vent_from_env()
    closer_function = LinearVentFunction(VENT_CLOSE_TIME)
    machine = init_state_machine(mqtt_client, hardware, vent, closer_function)

    # Initialize thermal camera and StoveLink encoder
    thermal_camera = get_thermal_camera(hardware.i2c, allow_mock=True)
    stovelink_encoder = StoveLinkEncoder()
    last_camera_update = 0

    # Initialize thermal camera recovery manager
    def thermal_camera_factory():
        # return get_thermal_camera(hardware.i2c, allow_mock=False)
        thermal_camera.reinitialize()
        return thermal_camera

    thermal_recovery = I2CDeviceRecoveryManager(
        thermal_camera_factory,
        device_name="MLX90640 Thermal Camera",
        base_delay=5,
        max_delay=300,
    )
    thermal_recovery.device = thermal_camera

    # Attempt initial MQTT connection
    try:
        logger.info("Connecting to MQTT...")
        mqtt_client.will_set(mqtt_topic + "/status", "offline")
        mqtt_client.connect()
        hardware.led_off()
    except MMQTTException as e:
        logger.error("Failed to connect to MQTT: %s", e)

    # Integrate with Home Assistant
    ha = HomeAssistant(
        machine,
        mqtt_topic,
        wifi.radio.hostname,
        measurement_buffer_interval=MEASUREMENT_BUFFER_INTERVAL,
    )
    discovery = ha.mqtt_discovery()
    mqtt_client.publish(discovery["topic"], discovery["message"])
    def setup_ha_handlers(mqtt_client: MQTT, ha: HomeAssistant):
        ha_handlers = ha.get_command_handlers()
        for topic in ha_handlers:
            logger.debug("subscribing to %s", topic)
            mqtt_client.subscribe(topic)
        mqtt_message_handlers.update(ha_handlers)
        mqtt_client.publish(mqtt_topic + "/status", "online")
    
    setup_ha_handlers(mqtt_client, ha)
    encoder_status = check_encoder(hardware)
    ha.send_encoder_status(*encoder_status)
    ha.update_camera_ok(True)
    machine.set_state("idle")
    logger.info("Starting main loop")

    # Main loop
    mqtt_exception_raised = False
    camera_exception_raised = False
    oserror_exception_raised: set[str] = set()
    unexpected_exception_raised: set[str] = set()
    while True:
        current_time = time.monotonic()

        # Priority 1: Ensure WiFi is connected
        wifi_manager.check_and_recover(current_time)

        # Priority 2: Ensure MQTT is connected (only if WiFi is OK)
        if mqtt_conn_manager.attempt_reconnect(current_time, mqtt_exception_raised):
            hardware.led_off()
            if mqtt_exception_raised:
                mqtt_handler.resume()
                ha.clear_cached_state()
                setup_ha_handlers(mqtt_client, ha)
            mqtt_exception_raised = False

        # Priority 3: Run state machine
        try:
            machine.update()

            if ha.refresh_discovery:
                logger.info("Re-publishing HA discovery payload and state topics")
                discovery = ha.mqtt_discovery()
                mqtt_client.publish(discovery["topic"], discovery["message"])
                # Give HA time to subscribe to availability topic
                time.sleep(0.25)
                encoder_status = check_encoder(hardware)
                mqtt_client.publish(ha.topic_prefix + "/status", "online")
                ha.send_encoder_status(*encoder_status)
                ha.update_camera_ok(not camera_exception_raised)
                ha.refresh_discovery = False
            ha.update()

            # Priority 4: Update thermal camera at configured interval
            if current_time - last_camera_update >= THERMAL_CAMERA_INTERVAL:

                try:
                    if do_thermal_camera_stuff(camera_exception_raised):
                        camera_exception_raised = False
                    last_camera_update = current_time

                except OSError as e:
                    # I2C communication error with thermal camera (e.g., errno 32: broken pipe)
                    thermal_recovery.report_error(e)
                    last_camera_update = (
                        current_time - THERMAL_CAMERA_INTERVAL + 1 # Update time to prevent rapid retries
                    )
                    camera_exception_raised = True
                    ha.update_camera_ok(False)

        except MMQTTException as e:
            # Log but don't try to reconnect here - let managers handle it
            # Use the mqtt_logger with file and stream handler as normal logger has mqtt_handler
            mqtt_handler.suspend()
            logger.error("MQTT error during state update: %s", e)
            hardware.led_on()
            mqtt_exception_raised = True
        except OSError as e:
            # Network/IO errors not related to thermal camera
            mqtt_handler.suspend()
            logger.error("Network/IO error during state update: %s", e)
            hardware.led_on()
            mqtt_exception_raised = True
            # Log the stack trace exactly once
            if str(e) not in oserror_exception_raised:
                import traceback
                stack_trace = traceback.format_exception(e, chain=True)
                logger.error("OSError stack trace: %s\n%s", str(e), '\n'.join(stack_trace))
                oserror_exception_raised.add(str(e))
        except Exception as e:
            mqtt_logger.error("Unexpected error in main loop: %s", e)
            hardware.led_on()
            # Log the stack trace exactly once
            if str(e) not in unexpected_exception_raised:
                import traceback
                stack_trace = traceback.format_exception(e, chain=True)
                mqtt_logger.error("Unexpected error stack trace: %s\n%s", str(e), '\n'.join(stack_trace))
                unexpected_exception_raised.add(str(e))

        # Small sleep to prevent busy-waiting
        time.sleep(0.1)
