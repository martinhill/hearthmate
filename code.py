import time
import os
import wifi
import socketpool
import ssl

from adafruit_minimqtt.adafruit_minimqtt import MQTT, MMQTTException
import adafruit_logging as logging

from state_machine import StateMachine, State
from hw_test import TestMotion, logger as test_logger
from hardware import get_hardware, logger as hw_logger
from airvent import create_vent_from_env
from vent_closer import VentCloser, LinearVentFunction, logger as vent_logger
from vent_mover import MoveVentState, logger as vm_logger
from logging import MQTTHandler, FileHandler
from connections import WiFiConnectionManager, MQTTConnectionManager, logger as conn_logger
from homeassistant import HomeAssistant, logger as ha_logger
from thermal_camera import get_thermal_camera, logger as cam_logger
from stovelink import StoveLinkEncoder, logger as stovelink_logger

VENT_CLOSE_TIME = os.getenv("VENT_CLOSE_TIME", 60*60)
THERMAL_CAMERA_INTERVAL = int(os.getenv("THERMAL_CAMERA_INTERVAL", 30))  # seconds
THERMAL_MAX_TEMP_CHANGE = float(os.getenv("THERMAL_MAX_TEMP_CHANGE", 30.0))  # degrees C

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
    def __init__(self, sensitivity = 0.001):
        super().__init__("idle")
        self.detected_fully_closed = False
        self.sensitivity = sensitivity

    def enter(self, machine):
        hardware = machine.data["hardware"]
        hardware.motor.release()
        hardware.set_pixel_color((0,32,32)) # teal
        vent.update_from_hardware(hardware.read_raw_angle())
        self.last_position = vent.get_position()
        logger.info("Idle")

    def resume(self, machine):
        hardware = machine.data["hardware"]
        hardware.motor.release()
        if self.detected_fully_closed:
            hardware.set_pixel_color((0x8f,0x8f,0)) # yellow
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
        if  vent_position > 0.99 and not self.detected_fully_closed:
            # first detection of vent closed
            self.detected_fully_closed = True
            hardware.set_pixel_color((0x8f,0x8f,0)) # yellow
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
    mqtt_host=os.getenv('MQTT_HOST'),
    mqtt_user=os.getenv('MQTT_USER'),
    mqtt_password=os.getenv('MQTT_PASSWORD'),
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
        logger.info("Connected to %s! Listening for commands on %s", mqtt_host, command_topic)
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


def init_state_machine(mqtt_client, hardware, vent):

    # Create the state machine
    machine = StateMachine()
    machine.data["hardware"] = hardware
    machine.data["mqtt_client"] = mqtt_client
    machine.data["vent"] = vent
    machine.add_state(TestMotion(moves_each_direction=2, target_step_angle=30.0))
    machine.add_state(IdleState())
    machine.add_state(MoveVentState())
    machine.add_state(VentCloser(LinearVentFunction(VENT_CLOSE_TIME)))
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
        logger.warning("Encoder status check: magnet detected but too %s (STATUS=0x%X)", magnet_condition, encoder_status)
    else:
        logger.error("Encoder status check failed: STATUS=0x%X", encoder_status)
    return (encoder_md, encoder_ml, encoder_mh)

def setup_loggers(mqtt_client: MQTT):
    mqtt_handler = MQTTHandler(mqtt_client, mqtt_topic + "/log")
    timestamp_formatter = logging.Formatter(fmt="%(asctime)s %(levelname)s %(name)s: %(message)s")
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
    mqtt_logger.setLevel(getattr(logging, os.getenv("MQTT_LOG_LEVEL", "INFO"), logging.INFO))


def get_combustion_time(machine: StateMachine) -> int:
    """
    Get elapsed combustion time in seconds.
    
    Args:
        machine: State machine instance
        
    Returns:
        int: Seconds since burn cycle started (0 if not in vent_closer state)
    """
    if machine.current_state == "vent_closer":
        vent_closer = machine.states.get("vent_closer")
        if vent_closer and vent_closer.machine.data.get("function"):
            function = vent_closer.machine.data["function"]
            elapsed = function.get_elapsed_time()
            return int(max(0, elapsed))
    return 0


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
    mqtt_message_handlers = {
        command_topic: command_handler
    }
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
    setup_loggers(mqtt_client)

    # Get the hardware interface
    hardware = get_hardware()
    hardware.led_on()
    vent = create_vent_from_env()
    machine = init_state_machine(mqtt_client, hardware, vent)
    
    # Initialize thermal camera and StoveLink encoder
    thermal_camera = get_thermal_camera(hardware.i2c)
    stovelink_encoder = StoveLinkEncoder()
    last_camera_update = 0

    # Attempt initial MQTT connection
    try:
        logger.info("Connecting to MQTT...")
        mqtt_client.will_set(mqtt_topic + "/status", "offline")
        mqtt_client.connect()
        hardware.led_off()
    except MMQTTException as e:
        logger.error("Failed to connect to MQTT: %s", e)

    # Integrate with Home Assistant
    ha = HomeAssistant(machine, mqtt_topic, wifi.radio.hostname)
    discovery = ha.mqtt_discovery()
    ha_handlers = ha.get_command_handlers()
    for topic in ha_handlers:
        logger.debug("subscribing to %s", topic)
        mqtt_client.subscribe(topic)
    mqtt_message_handlers.update(ha_handlers)
    mqtt_client.publish(discovery["topic"], discovery["message"])
    mqtt_client.publish(mqtt_topic + "/status", "online")

    encoder_status = check_encoder(hardware)
    ha.send_encoder_status(*encoder_status)
    machine.set_state("idle")
    logger.info("Starting main loop")

    # Main loop
    mqtt_exception_raised = False
    while True:
        current_time = time.monotonic()

        # Priority 1: Ensure WiFi is connected
        wifi_manager.check_and_recover(current_time)

        # Priority 2: Ensure MQTT is connected (only if WiFi is OK)
        if mqtt_conn_manager.attempt_reconnect(current_time, mqtt_exception_raised):
            hardware.led_off()
            if mqtt_exception_raised:
                mqtt_client.publish(mqtt_topic + "/status", "online")
                ha.clear_cached_state()
            mqtt_exception_raised = False

        # Priority 3: Run state machine
        try:
            machine.update()
            ha.update()
            
            # Priority 4: Update thermal camera at configured interval
            if current_time - last_camera_update >= THERMAL_CAMERA_INTERVAL:
                camera_start_time = time.monotonic()
                frame = thermal_camera.capture_frame()
                if frame:
                    # Calculate temperature statistics
                    stats_start_time = time.monotonic()
                    np_frame = thermal_camera.get_np_frame()
                    stats = thermal_camera.get_temperature_statistics(np_frame)
                    # stats = thermal_camera.get_temperature_statistics(frame)
                    
                    # Validate stats to filter out erroneous readings
                    if ha.validate_thermal_stats(stats, THERMAL_MAX_TEMP_CHANGE):
                        # Stats are valid - publish image and statistics
                        
                        # Encode and publish StoveLink binary packet (use numpy frame for efficiency)
                        stovelink_start = time.monotonic()
                        vent_position = vent.get_position()
                        combustion_time = get_combustion_time(machine)
                        stovelink_packet = stovelink_encoder.encode_packet(
                            np_frame, 
                            vent_position, 
                            combustion_time
                        )
                        mqtt_client.publish(mqtt_topic + "/stovelink", stovelink_packet)
                        
                        camera_end_time = time.monotonic()
                        stovelink_time = camera_end_time - stovelink_start
                        camera_process_time = camera_end_time - camera_start_time
                        capture_time = stats_start_time - camera_start_time
                        stats_calc_time = stovelink_start - stats_start_time
                        logger.debug(
                            "Stats: %.1f %.1f %.1f %.1f, time=%.4fs (cap=%.4fs, stat=%.4fs, sl=%.4fs)",
                            stats['min'], stats['max'], stats['mean'], stats['median'],
                            camera_process_time, capture_time, stats_calc_time, stovelink_time
                        )
                        ha.update_thermal_statistics(stats)
                    # If invalid, validate_thermal_stats already logged the warning
                last_camera_update = current_time
                
        except MMQTTException as e:
            # Log but don't try to reconnect here - let managers handle it
            # Use the mqtt_logger with file and stream handler as normal logger has mqtt_handler
            mqtt_logger.error("MQTT error during state update: %s", e)
            hardware.led_on()
            mqtt_exception_raised = True
        except OSError as e:
            mqtt_logger.error("Network/IO error during state update: %s", e)
            hardware.led_on()
        # except Exception as e:
        #     mqtt_logger.error("Unexpected error in main loop: %s", e)

        # Small sleep to prevent busy-waiting
        time.sleep(0.1)

