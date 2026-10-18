import time
import os
import wifi
import socketpool
import ssl
import gc
import supervisor
import microcontroller
from watchdog import WatchDogMode

from adafruit_minimqtt.adafruit_minimqtt import MQTT, MMQTTException, MMQTTStateError
import adafruit_logging as logging

from state_machine import StateMachine, State, logger as sm_logger
from hw_test import TestMotion, logger as test_logger
from hardware import get_hardware, logger as hw_logger
from airvent import create_vent_from_env
from vent_closer import (
    VentCloser,
    LinearVentFunction,
    VentFunctionABC,
    logger as vent_logger,
)
from vent_mover import MoveVentState, logger as vm_logger
from temperature_target import TemperatureTargetFunction, logger as tt_logger
from burn_state import BurnStateStore, run_reason, logger as bs_logger
from logging import MQTTHandler, FileHandler
from connections import (
    WiFiConnectionManager,
    MQTTConnectionManager,
    I2CDeviceRecoveryManager,
    logger as conn_logger,
)
from homeassistant import HomeAssistant, logger as ha_logger
from thermal_camera import (
    ThermalCamera,
    get_thermal_camera,
    logger as cam_logger,
    adafruit_mlx90640,
)
from stovelink import StoveLinkEncoder, logger as stovelink_logger

VENT_CLOSE_TIME: int = int(os.getenv("VENT_CLOSE_TIME", 60 * 60))
THERMAL_CAMERA_INTERVAL: int = int(os.getenv("THERMAL_CAMERA_INTERVAL", 30))  # seconds
THERMAL_MAX_TEMP_CHANGE: float = float(
    os.getenv("THERMAL_MAX_TEMP_CHANGE", 30.0)
)  # degrees C
MEASUREMENT_BUFFER_INTERVAL: int = int(
    os.getenv("MEASUREMENT_BUFFER_INTERVAL", 15)
)  # seconds
# Frames whose chess-pattern subpages differ by more than this are not used for control
THERMAL_SUBPAGE_THRESHOLD: float = float(
    os.getenv("THERMAL_SUBPAGE_THRESHOLD", 10.0)
)  # degrees C
# Former mode names still accepted in MQTT commands and AUTO_MODE
MODE_ALIASES = {"vent_closer": "timed_close", "close": "timed_close"}
# Mode engaged automatically when Idle sees the vent closed then opened: timed_close or temp_control
AUTO_MODE: str = os.getenv("AUTO_MODE", "timed_close")
AUTO_MODE = MODE_ALIASES.get(AUTO_MODE, AUTO_MODE)

# Reload when files are written over the web workflow. Off, so a deploy during a burn reloads
# once, on the reload command, after all files are uploaded (scripts/deploy.sh).
AUTORELOAD: bool = os.getenv("AUTORELOAD", "off").lower() in ("on", "true", "1")

# Reset the board if the main loop stops for this long (s); 0 disables. The board has frozen
# for good inside a network call, still answering ping; the 60-80 s stalls recover by themselves.
# The burn resumes after a watchdog reset if the device clock survived it (burn_state.py).
WATCHDOG_TIMEOUT: float = float(os.getenv("WATCHDOG_TIMEOUT", 120))

MQTT_TIMEOUT: float = float(os.getenv("MQTT_TIMEOUT", "0.1"))
# Re-announce availability this often (s), so a stale "offline" can't stick
STATUS_INTERVAL: int = 60
MQTT_KEEP_ALIVE: int = int(os.getenv("MQTT_KEEP_ALIVE", 300))

logger = logging.getLogger(__name__)
log_level = getattr(logging, os.getenv("LOG_LEVEL", "INFO"), logging.INFO)
file_log_level = getattr(logging, os.getenv("FILE_LOG_LEVEL", "INFO"), logging.INFO)
logger.setLevel(log_level)
stream_handler = logging.StreamHandler()
logger.addHandler(stream_handler)
mqtt_logger = logging.getLogger("mqtt")

# Timing threshold for warning about slow operations (seconds)
SLOW_OPERATION_THRESHOLD = 10.0


def log_slow_operation(operation_name, duration, threshold=SLOW_OPERATION_THRESHOLD):
    """Log a warning if an operation exceeds the threshold duration."""
    if duration > threshold:
        logger.warning(
            "Slow operation: %s took %.1fs (threshold: %.1fs)",
            operation_name,
            duration,
            threshold,
        )


class IdleState(State):
    """
    Idle state.
    If vent movement to fully closed position is detected, and then fully open,
    the state machine will transition to the AUTO_MODE state (timed_close or temp_control).
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
        # Always check MQTT --when vent position is stable--
        # if abs(vent_position - self.last_position) < self.sensitivity:
        machine.mqtt_loop()

        vent.update_from_hardware(hardware.read_raw_angle())
        vent_position = vent.get_position()
        self.last_position = vent_position
        if vent_position > 0.99 and not self.detected_fully_closed:
            # first detection of vent closed
            self.detected_fully_closed = True
            hardware.set_pixel_color((0x8F, 0x8F, 0))  # yellow
            logger.info("Detected vent closed")
        elif vent_position < 0.01 and self.detected_fully_closed:
            # detected vent fully open after closed position - transition
            logger.info("Detected vent open - engaging %s", AUTO_MODE)
            machine.set_state(AUTO_MODE)


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


mqtt_topic: str = os.getenv("MQTT_TOPIC")
command_topic: str = mqtt_topic + "/command"


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
        socket_timeout=MQTT_TIMEOUT,
        keep_alive=MQTT_KEEP_ALIVE,
        # Fixed, not MiniMQTT's random default: after a hard reset the broker then replaces the
        # dead session at once, instead of publishing its "offline" will minutes after the new
        # session announced "online" (HA showed the device unavailable until the next restart)
        client_id="hearthmate-" + mqtt_topic,
    )

    def connected(client, userdata, flags, rc):
        # This function will be called when the client is connected
        # successfully to the broker.
        logger.info(
            "Connected to %s! Listening for commands on %s", mqtt_host, command_topic
        )
        # Subscribe to all changes on the onoff_feed.
        client.subscribe(command_topic)
        client.publish(mqtt_topic + "/status", "online")

    def disconnected(client, userdata, rc):
        # This method is called when the client is disconnected
        logger.warning("Disconnected from %s!", mqtt_host)

    # Setup the callback methods above
    mqtt_client.on_connect = connected
    mqtt_client.on_disconnect = disconnected
    mqtt_client.on_message = message_callback
    mqtt_client.logger = mqtt_logger

    return mqtt_client


def init_state_machine(mqtt_client, hardware, vent, closer_function, temp_function):
    # Create the state machine
    machine = StateMachine(mqtt_timeout=MQTT_TIMEOUT)
    machine.data["hardware"] = hardware
    machine.data["mqtt_client"] = mqtt_client
    machine.data["vent"] = vent
    machine.add_state(TestMotion(moves_each_direction=2, target_step_angle=30.0))
    machine.add_state(IdleState())
    machine.add_state(MoveVentState())
    machine.add_state(VentCloser(closer_function, name="timed_close"))
    machine.add_state(VentCloser(temp_function, name="temp_control"))
    return machine


def check_encoder(hardware):
    encoder_status = hardware.read_encoder_status()
    encoder_md = bool(encoder_status & 0x20)
    encoder_ml = bool(encoder_status & 0x10)
    encoder_mh = bool(encoder_status & 0x8)
    if not hardware.is_mock:
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
        adafruit_mlx90640.logger: [mqtt_handler, file_handler],
        stovelink_logger: [mqtt_handler, file_handler],
        tt_logger: [mqtt_handler, file_handler],
        bs_logger: [mqtt_handler, file_handler],
        sm_logger: [mqtt_handler, file_handler],
    }
    for lgr, handlers in logger_handlers_mapping.items():
        for handler in handlers:
            lgr.addHandler(handler)
        lgr.setLevel(log_level)
    adafruit_mlx90640.logger.setLevel(logging.INFO)  ## too noisy
    mqtt_logger.setLevel(
        getattr(logging, os.getenv("MQTT_LOG_LEVEL", "INFO"), logging.INFO)
    )


def get_active_function(machine: StateMachine) -> VentFunctionABC:
    """
    Get the vent function of the current mode (timed_close or temp_control).
    Falls back to the linear closer function when neither mode is active.
    """
    for name in machine.state_stack + [machine.current_state]:
        if name in ("timed_close", "temp_control"):
            return machine.states[name].machine.data["function"]
    return closer_function


def get_combustion_time(function: VentFunctionABC) -> int:
    """
    Get elapsed combustion time in seconds.

    Args:
        function: Vent function of the active mode

    Returns:
        int: Seconds since burn cycle started
    """
    elapsed = function.get_elapsed_time()
    return int(max(0, elapsed))


def do_thermal_camera_stuff(camera_exception_raised):
    """
    Do all the thermal camera stuff here to keep the main loop clean.
    Returns True if successful, False if camera recovery is needed.
    """
    global thermal_camera, ha, mqtt_client, vent, stovelink_encoder, machine, mqtt_topic
    global temp_function

    if camera_exception_raised:
        recovery_start = time.monotonic()
        recovered_camera = thermal_recovery.attempt_recovery(current_time)
        recovery_duration = time.monotonic() - recovery_start
        if recovery_duration > 1.0:
            logger.warning(
                "Thermal camera recovery attempt took %.1fs", recovery_duration
            )
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
    capture_duration = time.monotonic() - camera_start_time
    if capture_duration > 5.0:
        logger.warning(
            "Thermal camera frame capture took %.1fs (retries: %d)",
            capture_duration,
            thermal_camera.retry_count,
        )
    if frame:
        # Calculate temperature statistics
        # Note: get_np_frame() reuses internal numpy array to avoid allocations
        # This eliminates 3KB allocation every 10s, reducing heap fragmentation
        stats_start_time = time.monotonic()
        np_frame = thermal_camera.get_np_frame()
        stats = thermal_camera.get_temperature_statistics(np_frame)

        # Feed the temperature target controller, skipping chessboard-artifact frames
        mismatch = thermal_camera.subpage_mismatch(np_frame)
        if mismatch < THERMAL_SUBPAGE_THRESHOLD:
            temp_function.update_temperature(stats["max"])
        else:
            logger.warning("Thermal frame subpage mismatch %.1fC - not used for control", mismatch)

        # Encode and publish StoveLink binary packet (use numpy frame for efficiency)
        tmp36_start = time.monotonic()
        tmp36_temp = hardware.tmp36_temperature_C()
        stovelink_start = time.monotonic()
        tmp36_time = stovelink_start - tmp36_start
        stats_calc_time = stovelink_start - stats_start_time
        capture_time = stats_start_time - camera_start_time
        vent_position = vent.get_position()
        combustion_time = get_combustion_time(get_active_function(machine))
        stovelink_packet = stovelink_encoder.encode_packet(
            np_frame, vent_position, combustion_time, tmp36_temp
        )
        try:
            stovelink_publish_start = time.monotonic()
            mqtt_client.publish(mqtt_topic + "/stovelink", stovelink_packet)

            # Validate stats to warn of erroneous readings
            ha_stats_start = time.monotonic()
            if ha.validate_thermal_stats(stats, THERMAL_MAX_TEMP_CHANGE):
                ha.update_thermal_statistics(stats)
            camera_end_time = time.monotonic()
            camera_process_time = camera_end_time - camera_start_time
            stovelink_packet_time = stovelink_publish_start - stovelink_start
            if camera_process_time > SLOW_OPERATION_THRESHOLD:
                logger.warning(
                    "Camera processing took %.1fs (cap=%.2fs, stat=%.2fs, tmp36=%.2fs, sl=%.2fs, mqtt=%.2fs, ha=%.2fs)",
                    camera_process_time,
                    capture_time,
                    stats_calc_time,
                    tmp36_time,
                    stovelink_packet_time,
                    ha_stats_start - stovelink_publish_start,
                    camera_end_time - ha_stats_start,
                )
        except MMQTTStateError as e:
            logger.warning(
                "MMQTTStateError during StoveLink MQTT packet publication: %s", e
            )
        except OSError as e:
            # Wrap OSError in MMQTTException to be handled by the main loop
            # Otherwise it would trigger camera recovery manager
            raise MMQTTException(
                f"OSError during StoveLink MQTT packet publication: {e}"
            )
    return True

def check_mem_free(threshold : int = int(os.getenv("LOW_MEMORY_THRESHOLD", 50000))):
    # Memory diagnostics: Check free memory

    mem_free = gc.mem_free()
    if mem_free < threshold:  # Low memory threshold
        logger.warning("Low memory: %d bytes", mem_free)
    return mem_free

def resume_or_idle():
    """Resume the burn after a soft reboot from the retained saved state, otherwise start in
    idle (see burn_state.py)"""
    reason = run_reason()
    saved = None
    if mqtt_client.is_connected():
        mqtt_message_handlers[burn_store.topic] = burn_store.on_message
        try:
            saved = burn_store.fetch()
        except (MMQTTException, OSError) as e:
            logger.warning("Could not fetch the saved burn state: %s", e)
        del mqtt_message_handlers[burn_store.topic]
    vent.update_from_hardware(hardware.read_raw_angle())
    resume, why = burn_store.check(saved, reason, vent.get_position())
    if resume:
        try:
            downtime = burn_store.restore(machine, saved)
            # Count the frames missed while down, so the sequence shows the gap
            stovelink_encoder.sequence_id = (
                saved["seq"] + 1 + int(downtime / THERMAL_CAMERA_INTERVAL)
            )
            logger.info("Resumed %s", why)
            return
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            why = "saved state incomplete (%s: %s)" % (type(e).__name__, e)
    logger.info("Starting in idle: %s", why)
    machine.set_state("idle")
    if saved is not None:
        burn_store.clear()


def reload_device():
    "Save the burn state and reload, to run newly deployed code (the reload command)"
    logger.info("Reloading on request")
    try:
        burn_store.save(machine, stovelink_encoder.sequence_id)
        mqtt_client.loop(timeout=0.5)
    except (MMQTTException, OSError) as e:
        logger.warning("Could not save the burn state before reloading: %s", e)
    # If the new code fails before its main loop, a fixed upload must still reload it
    supervisor.runtime.autoreload = True
    supervisor.reload()


if __name__ == "__main__":

    def command_handler(message):
        global machine
        message = MODE_ALIASES.get(message, message)
        if message == "test":
            machine.set_state("test_motion")
        elif message in ("timed_close", "temp_control"):
            machine.set_state(message)
        elif message == "stop" or message == "idle":
            machine.set_state("idle")
        elif message == "reload":
            reload_device()
        elif message == "test_watchdog":
            # Stop feeding the watchdog, as a hung main loop would
            logger.warning("Testing the watchdog: main loop stopped, reset in %ds", WATCHDOG_TIMEOUT)
            while True:
                time.sleep(1)
        elif message == "collect":
            logger.info("Free memory before GC: %d bytes", gc.mem_free())
            gc.collect()
            logger.info("Free memory after GC: %d bytes", gc.mem_free())
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
    # Logged once MQTT is up, so it reaches the burn log too
    wifi_power_management = wifi_manager.apply_power_management()

    # Get the hardware interface
    hardware = get_hardware()
    hardware.led_on()
    vent = create_vent_from_env()
    closer_function = LinearVentFunction(VENT_CLOSE_TIME)
    temp_function = TemperatureTargetFunction(time_range=VENT_CLOSE_TIME)
    machine = init_state_machine(mqtt_client, hardware, vent, closer_function, temp_function)

    # Initialize thermal camera and StoveLink encoder
    thermal_camera: ThermalCamera = get_thermal_camera(hardware.i2c)
    stovelink_encoder = StoveLinkEncoder()
    last_camera_update = 0

    # Initialize thermal camera recovery manager
    def thermal_camera_factory():
        # A plain reinitialize first. A hung camera only responds again after a power cycle (as a
        # soft reboot does), so from the second attempt on, cut power to its STEMMA QT port first.
        if thermal_recovery.recovery_failures >= 2:
            thermal_camera.power_cycle()
        thermal_camera.reinitialize()
        return thermal_camera

    # temp_control depends on the camera, so retry at least every minute
    thermal_recovery = I2CDeviceRecoveryManager(
        thermal_camera_factory,
        device_name="MLX90640 Thermal Camera",
        base_delay=5,
        max_delay=60,
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

    logger.info("Step parameters: %s", hardware.step_params)
    logger.info("WiFi power management: %s", wifi_power_management)

    # Integrate with Home Assistant
    logger.info("Setting up Home Assistant...")
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
        time.sleep(0.2)
        mqtt_client.publish(mqtt_topic + "/status", "online")

    setup_ha_handlers(mqtt_client, ha)
    encoder_status = check_encoder(hardware)
    ha.send_encoder_status(*encoder_status)
    ha.update_camera_ok(True)

    burn_store = BurnStateStore(
        mqtt_client,
        mqtt_topic + "/controller/saved",
        {"timed_close": closer_function, "temp_control": temp_function},
        hardware,
    )
    resume_or_idle()
    logger.info("Starting main loop (autoreload %s)", "on" if AUTORELOAD else "off")

    # Main loop
    mqtt_exception_raised = False
    camera_exception_raised = False
    last_status_time = 0
    oserror_exception_raised: set[str] = set()
    unexpected_exception_raised: set[str] = set()
    try:
        # Off until the finally below. The flag survives soft reloads (CircuitPython only sets it
        # at a hard start), so every way out of here turns it back on.
        supervisor.runtime.autoreload = AUTORELOAD
        if WATCHDOG_TIMEOUT > 0:
            microcontroller.watchdog.timeout = WATCHDOG_TIMEOUT
            microcontroller.watchdog.mode = WatchDogMode.RESET
            logger.info("Watchdog: reset after %ds without a main loop iteration", WATCHDOG_TIMEOUT)
        while True:
            if WATCHDOG_TIMEOUT > 0:
                microcontroller.watchdog.feed()
            current_time = time.monotonic()

            # Priority 1: Ensure WiFi is connected
            wifi_start = time.monotonic()
            wifi_manager.check_and_recover(current_time)
            log_slow_operation("WiFi check/recovery", time.monotonic() - wifi_start)

            # Priority 2: Ensure MQTT is connected (only if WiFi is OK)
            mqtt_reconnect_start = time.monotonic()
            if mqtt_conn_manager.attempt_reconnect(current_time, mqtt_exception_raised):
                hardware.led_off()
                if mqtt_exception_raised:
                    mqtt_handler.resume()
                    ha.clear_cached_state()
                    setup_ha_handlers(mqtt_client, ha)
                mqtt_exception_raised = False
            log_slow_operation(
                "MQTT reconnect attempt", time.monotonic() - mqtt_reconnect_start
            )

            # Priority 3: Run state machine
            try:
                machine.update()

                if ha.refresh_discovery:
                    discovery_start = time.monotonic()
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
                    log_slow_operation(
                        "HA discovery refresh", time.monotonic() - discovery_start
                    )

                ha_update_start = time.monotonic()
                ha.update()
                log_slow_operation("HA update", time.monotonic() - ha_update_start)

                save_start = time.monotonic()
                burn_store.maybe_save(machine, stovelink_encoder.sequence_id)
                log_slow_operation("Burn state save", time.monotonic() - save_start)

                if current_time - last_status_time >= STATUS_INTERVAL:
                    mqtt_client.publish(mqtt_topic + "/status", "online")
                    last_status_time = current_time

                # Priority 4: Update thermal camera at configured interval
                if current_time - last_camera_update >= THERMAL_CAMERA_INTERVAL:
                    try:
                        # First, check and monitor free memory. Done here to minimize overhead.
                        ha.update_mem_free(check_mem_free())
                        # The actual thermal camera update
                        thermal_start = time.monotonic()
                        if do_thermal_camera_stuff(camera_exception_raised):
                            camera_exception_raised = False
                        log_slow_operation(
                            "Thermal camera capture", time.monotonic() - thermal_start
                        )
                        last_camera_update = current_time

                    except OSError as e:
                        # I2C communication error with thermal camera (e.g., errno 32: broken pipe)
                        thermal_recovery.report_error(e)
                        last_camera_update = (
                            current_time
                            - THERMAL_CAMERA_INTERVAL
                            + 1  # Update time to prevent rapid retries
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
                    logger.error(
                        "OSError stack trace: %s\n%s", str(e), "\n".join(stack_trace)
                    )
                    oserror_exception_raised.add(str(e))
            except Exception as e:
                mqtt_logger.error("Unexpected error in main loop: %s", e)
                hardware.led_on()
                # Log the stack trace exactly once
                if str(e) not in unexpected_exception_raised:
                    import traceback

                    stack_trace = traceback.format_exception(e, chain=True)
                    mqtt_logger.error(
                        "Unexpected error stack trace: %s\n%s",
                        str(e),
                        "\n".join(stack_trace),
                    )
                    unexpected_exception_raised.add(str(e))

            # Log slow main loop iterations
            loop_duration = time.monotonic() - current_time
            log_slow_operation("Main loop iteration", loop_duration)

            # Small sleep to prevent busy-waiting. CircuitPython runs its background tasks (WiFi, web
            # workflow, mDNS) during the sleep, so a network stall shows up here, not in a timed step.
            sleep_start = time.monotonic()
            time.sleep(0.1)
            sleep_duration = time.monotonic() - sleep_start
            if sleep_duration > 5.0:
                logger.warning(
                    "Main loop stalled %.1fs in background tasks (RSSI %s)",
                    sleep_duration,
                    wifi.radio.ap_info.rssi if wifi.radio.ap_info else "-",
                )
    finally:
        # Leaving the main loop (Ctrl-C, a reload, an error): let file writes reload the device
        # again, so a fix can still be deployed, and save the state for the next run
        supervisor.runtime.autoreload = True
        if WATCHDOG_TIMEOUT > 0:
            # Not at the REPL; a soft reload clears it anyway
            try:
                microcontroller.watchdog.deinit()
            except Exception:
                pass
        try:
            burn_store.save(machine, stovelink_encoder.sequence_id)
        except Exception:
            pass
