import os
import json
import wifi
import adafruit_logging as logging

try:
    from typing import Callable
except ImportError:
    pass

from state_machine import StateMachine
from airvent import Vent, create_vent_from_env
from measurement_buffer import MeasurementBuffer

logger = logging.getLogger(__name__)

discovery_prefix_default = os.getenv("HA_DISCOVERY_PREFIX", "homeassistant")


class HomeAssistant:
    def __init__(
        self,
        machine: StateMachine,
        topic_prefix: str,
        device_name: str,
        discovery_prefix: str = discovery_prefix_default,
        closed_threshold=3,
        measurement_buffer_interval: int = 15,
    ):
        self.discovery_prefix = discovery_prefix
        self.device_name = device_name
        self.topic_prefix = topic_prefix
        self.machine: StateMachine = machine
        self.vent: Vent = machine.data["vent"]
        self.mqtt_client = machine.data["mqtt_client"]
        self.saved_values = {}
        self.closed_threshold = closed_threshold
        self.last_thermal_stats = None
        self.measurement_buffer = MeasurementBuffer(measurement_buffer_interval)

        # Use temporary Vent to determine the number of motor steps from open to closed
        # so we can match this to the range for the Home Assistant Valve integration
        temp_vent = create_vent_from_env()
        temp_vent.update_from_hardware(temp_vent.open_position)  # Fully open the valve
        steps, angle_delta, revs = temp_vent.close(1.0)  # Get steps to fully close
        self.position_open = steps

    def mqtt_discovery(self) -> dict[str, str]:
        discovery_topic = f"{self.discovery_prefix}/device/{self.device_name}/config"

        discovery_dict = {
            "dev": {
                "ids": self.device_name,
                "name": "Burnie",
            },
            "o": {
                "name": "Burnie",
            },
            "cmps": {
                "rssi": {
                    "name": "RSSI",
                    "p": "sensor",
                    "device_class": "signal_strength",
                    "unit_of_meas": "dBm",
                    "ent_cat": "diagnostic",
                    "unique_id": f"{self.device_name}_rssi",
                    "state_topic": f"{self.topic_prefix}/rssi/state",
                    "avty": [
                        {"topic": f"{self.topic_prefix}/status"},
                    ],
                },
                "encoder_magnet_detected": {
                    "name": "AS5600 magnet detected",
                    "p": "binary_sensor",
                    "ent_cat": "diagnostic",
                    "unique_id": f"{self.device_name}_encoder_md",
                    "state_topic": f"{self.topic_prefix}/encoder_md/state",
                    "icon": "mdi:magnet",
                    "avty": [
                        {"topic": f"{self.topic_prefix}/status"},
                    ],
                },
                "encoder_magnet_weak": {
                    "name": "AS5600 magnet weak",
                    "p": "binary_sensor",
                    "ent_cat": "diagnostic",
                    "unique_id": f"{self.device_name}_encoder_ml",
                    "state_topic": f"{self.topic_prefix}/encoder_ml/state",
                    "icon": "mdi:magnet",
                    "avty": [
                        {"topic": f"{self.topic_prefix}/status"},
                    ],
                },
                "encoder_magnet_strong": {
                    "name": "AS5600 magnet strong",
                    "p": "binary_sensor",
                    "ent_cat": "diagnostic",
                    "unique_id": f"{self.device_name}_encoder_mh",
                    "state_topic": f"{self.topic_prefix}/encoder_mh/state",
                    "icon": "mdi:magnet",
                    "avty": [
                        {"topic": f"{self.topic_prefix}/status"},
                    ],
                },
                "camera_ok": {
                    "name": "Camera OK",
                    "p": "binary_sensor",
                    "ent_cat": "diagnostic",
                    "unique_id": f"{self.device_name}_camera_ok",
                    "state_topic": f"{self.topic_prefix}/camera_ok/state",
                    "icon": "mdi:camera",
                    "avty": [
                        {"topic": f"{self.topic_prefix}/status"},
                    ],
                },
                "sequence_id": {
                    "name": "Stovelink Sequence",
                    "p": "sensor",
                    "ent_cat": "diagnostic",
                    "unique_id": f"{self.device_name}_seq_id",
                    "state_topic": f"{self.topic_prefix}/service/state",
                    "value_template": "{{ value_json.sequence_id }}",
                    "avty": [
                        {"topic": f"{self.topic_prefix}/status"},
                    ],
                },
                "timestamp_ms": {
                    "name": "Stovelink uptime",
                    "p": "sensor",
                    "device_class": "duration",
                    "unit_of_meas": "ms",
                    "ent_cat": "diagnostic",
                    "unique_id": f"{self.device_name}_timestamp_ms",
                    "state_topic": f"{self.topic_prefix}/service/state",
                    "value_template": "{{ value_json.timestamp_ms }}",
                    "avty": [
                        {"topic": f"{self.topic_prefix}/status"},
                    ],
                },
                "air_vent": {
                    "name": "Air Vent",
                    "p": "valve",
                    "unique_id": f"{self.device_name}_air_vent",
                    "reports_position": True,
                    "position_open": self.position_open,
                    "~": f"{self.topic_prefix}/air_vent",
                    "state_topic": "~/state",
                    "command_topic": "~/set",
                    "avty": [
                        {"topic": f"{self.topic_prefix}/status"},
                    ],
                },
                "operating_state": {
                    "name": "State Machine",
                    "p": "select",
                    "unique_id": f"{self.device_name}_state",
                    "command_topic": f"{self.topic_prefix}/command",
                    "state_topic": f"{self.topic_prefix}/state",
                    "options": [
                        "idle",
                        "vent_closer",
                    ],
                    "avty": [
                        {"topic": f"{self.topic_prefix}/status"},
                    ],
                },
                "closing_duration": {
                    "name": "Closing Duration",
                    "p": "number",
                    "unique_id": f"{self.device_name}_duration",
                    "command_topic": f"{self.topic_prefix}/duration/set",
                    "state_topic": f"{self.topic_prefix}/duration/state",
                    "device_class": "duration",
                    "unit_of_meas": "minutes",
                    "min": 5,
                    "max": 60,
                    "avty": [
                        {"topic": f"{self.topic_prefix}/status"},
                    ],
                },
                "tmp36": {
                    "name": "TMP36",
                    "p": "sensor",
                    "device_class": "temperature",
                    "unit_of_meas": "°C",
                    "unique_id": f"{self.device_name}_tmp36",
                    "state_topic": f"{self.topic_prefix}/tmp36/state",
                    "avty": [
                        {"topic": f"{self.topic_prefix}/status"},
                    ],
                },
                "thermal_camera": {
                    "name": "Thermal Camera DR",
                    "p": "camera",
                    "unique_id": f"{self.device_name}_thermal_camera/dynamic",
                    "topic": f"{self.topic_prefix}/thermal_camera/dynamic",
                    "image_encoding": "b64",
                    "avty": [
                        {"topic": f"{self.topic_prefix}/status"},
                    ],
                },
                "thermal_camera_static": {
                    "name": "Thermal Camera SR",
                    "p": "camera",
                    "unique_id": f"{self.device_name}_thermal_camera/static",
                    "topic": f"{self.topic_prefix}/thermal_camera/static",
                    "image_encoding": "b64",
                    "avty": [
                        {"topic": f"{self.topic_prefix}/status"},
                    ],
                },
                "thermal_temp_min": {
                    "name": "Thermal Min Temperature",
                    "p": "sensor",
                    "device_class": "temperature",
                    "unit_of_meas": "°C",
                    "unique_id": f"{self.device_name}_thermal_temp_min",
                    "state_topic": f"{self.topic_prefix}/thermal/min/state",
                    "avty": [
                        {"topic": f"{self.topic_prefix}/status"},
                    ],
                },
                "thermal_temp_max": {
                    "name": "Thermal Max Temperature",
                    "p": "sensor",
                    "device_class": "temperature",
                    "unit_of_meas": "°C",
                    "unique_id": f"{self.device_name}_thermal_temp_max",
                    "state_topic": f"{self.topic_prefix}/thermal/max/state",
                    "avty": [
                        {"topic": f"{self.topic_prefix}/status"},
                    ],
                },
                "thermal_temp_mean": {
                    "name": "Thermal Mean Temperature",
                    "p": "sensor",
                    "device_class": "temperature",
                    "unit_of_meas": "°C",
                    "unique_id": f"{self.device_name}_thermal_temp_mean",
                    "state_topic": f"{self.topic_prefix}/thermal/mean/state",
                    "avty": [
                        {"topic": f"{self.topic_prefix}/status"},
                    ],
                },
                "thermal_temp_median": {
                    "name": "Thermal Median Temperature",
                    "p": "sensor",
                    "device_class": "temperature",
                    "unit_of_meas": "°C",
                    "unique_id": f"{self.device_name}_thermal_temp_median",
                    "state_topic": f"{self.topic_prefix}/thermal/median/state",
                    "avty": [
                        {"topic": f"{self.topic_prefix}/status"},
                    ],
                },
                "burn_time": {
                    "name": "Burn time",
                    "p": "sensor",
                    "device_class": "duration",
                    "unit_of_meas": "s",
                    "unique_id": f"{self.device_name}_burn_time",
                    "state_topic": f"{self.topic_prefix}/service/state",
                    "value_template": "{{ value_json.combustion_time }}",
                    "avty": [
                        {"topic": f"{self.topic_prefix}/status"},
                    ],
                },
            },
            "qos": 0,
        }

        discovery_json = json.dumps(discovery_dict)

        return {"topic": discovery_topic, "message": discovery_json}

    def ha_status(self, message):
        if message == "online":
            # Resend discovery
            discovery = self.mqtt_discovery()
            self.mqtt_client.publish(discovery["topic"], discovery["message"])
            self.mqtt_client.publish(self.topic_prefix + "/status", "online")

    def set_air_vent(self, message):
        "Attempt to move the air vent to the requested position"
        logger.debug("set_air_vent: %s", message)
        try:
            vent_position = 1.0 - float(message) / self.position_open
            vent_position = max(0.0, min(vent_position, 1.0))

            if self.machine.handle_move_request(vent_position):
                logger.info(
                    "Set vent request accepted: vent_position=%.3f", vent_position
                )
            else:
                logger.warning(
                    "Set vent request rejected by state %s", self.machine.current_state
                )

        except ValueError as ve:
            logger.error("set_air_vent: %s", ve)

    def set_duration(self, message):
        try:
            function = self.machine.states["vent_closer"].machine.data["function"]
            function.time_range = int(message) * 60
            logger.info("Set closing duration to %d seconds", function.time_range)
        except Exception as e:
            logger.error("set_duration: %s", e)

    def get_command_handlers(self) -> dict[str, Callable[[str], None]]:
        "Provide the callbacks for MQTT command_topics"
        return {
            f"{self.topic_prefix}/air_vent/set": self.set_air_vent,
            f"{self.topic_prefix}/duration/set": self.set_duration,
            f"{self.discovery_prefix}/status": self.ha_status,
        }

    def send_encoder_status(self, status_md, status_ml, status_mh):
        """Update encoder_magnet_detected, encoder_magnet_weak, and encoder_magnet_strong components"""
        state_string = lambda value: "ON" if value else "OFF"
        self.mqtt_client.publish(
            f"{self.topic_prefix}/encoder_md/state", state_string(status_md)
        )
        self.mqtt_client.publish(
            f"{self.topic_prefix}/encoder_ml/state", state_string(status_ml)
        )
        self.mqtt_client.publish(
            f"{self.topic_prefix}/encoder_mh/state", state_string(status_mh)
        )

    def clear_cached_state(self):
        self.saved_values.clear()

    def update_mqtt_state(self, topic, value):
        "Send a single state update on a topic only if it changed since last"
        if self.saved_values.get(topic) != value:
            self.mqtt_client.publish(f"{self.topic_prefix}/{topic}", str(value))
            self.saved_values[topic] = value

    def update_thermal_camera(self, base64_image):
        """
        Publish thermal camera image to Home Assistant via MQTT.

        Args:
            base64_image: Base64-encoded image data
        """
        try:
            self.mqtt_client.publish(
                f"{self.topic_prefix}/thermal_camera", base64_image
            )
        except Exception as e:
            logger.error("Failed to publish thermal camera image: %s", e)

    def validate_thermal_stats(self, stats, max_change):
        """
        Validate thermal statistics against previous reading to detect erroneous data.

        Args:
            stats: Dictionary with keys: min, max, mean, median
            max_change: Maximum allowed change in degrees C for any statistic

        Returns:
            bool: True if stats are valid, False if they should be discarded
        """
        if self.last_thermal_stats is None:
            # First reading - accept it
            self.last_thermal_stats = stats
            return True

        # Check each statistic for excessive change
        for key in ["min", "max", "mean", "median"]:
            change = abs(stats[key] - self.last_thermal_stats[key])
            if change > max_change:
                logger.warning(
                    "Thermal stats discarded: %s changed by %.1fC (threshold: %.1fC) - "
                    "min=%.1fC max=%.1fC mean=%.1fC median=%.1fC",
                    key,
                    change,
                    max_change,
                    stats["min"],
                    stats["max"],
                    stats["mean"],
                    stats["median"],
                )
                return False

        # All stats within threshold - accept and update last reading
        self.last_thermal_stats = stats
        return True

    def update_thermal_statistics(self, stats):
        """
        Publish thermal camera temperature statistics to Home Assistant via MQTT.

        Args:
            stats: Dictionary with keys: min, max, mean, median
        """
        try:
            self.update_mqtt_state("thermal/min/state", f"{stats['min']:.1f}")
            self.update_mqtt_state("thermal/max/state", f"{stats['max']:.1f}")
            self.update_mqtt_state("thermal/mean/state", f"{stats['mean']:.1f}")
            self.update_mqtt_state("thermal/median/state", f"{stats['median']:.1f}")
        except Exception as e:
            logger.error("Failed to publish thermal statistics: %s", e)

    def update(self):
        "Update HA entities"
        # Buffer RSSI measurements
        try:
            rssi = wifi.radio.ap_info.rssi
            self.measurement_buffer.add_measurement("rssi", rssi)
            if self.measurement_buffer.should_publish("rssi"):
                rssi_avg = self.measurement_buffer.publish("rssi")
                self.update_mqtt_state("rssi/state", rssi_avg)
        except Exception as e:
            logger.error("sending rssi: %s", e)

        vent_position = self.vent.get_position()
        ha_vent = min(
            self.position_open, max(round(self.position_open * (1 - vent_position)), 0)
        )
        # Adjust vent position so almost closed appears closed
        if ha_vent < self.closed_threshold:
            ha_vent = 0

        hardware = self.machine.data["hardware"]
        self.update_mqtt_state("air_vent/state", str(ha_vent))
        self.update_mqtt_state("state", self.machine.current_state)

        # Buffer TMP36 temperature measurements
        if hardware.is_mock:
            # This will set the value to unknown in HA
            self.update_mqtt_state("tmp36/state", "None")
        else:
            tmp36_temp = hardware.tmp36_temperature_C()
            self.measurement_buffer.add_measurement("tmp36", tmp36_temp)
            if self.measurement_buffer.should_publish("tmp36"):
                tmp36_avg = self.measurement_buffer.publish("tmp36")
                self.update_mqtt_state("tmp36/state", tmp36_avg)

        # kludge alert
        vent_closer = self.machine.states.get("vent_closer")
        if vent_closer:
            function = vent_closer.machine.data.get("function")
            if function:
                duration = int(function.time_range / 60)
                self.update_mqtt_state("duration/state", duration)

    def update_camera_ok(self, ok):
        self.update_mqtt_state("camera_ok/state", "ON" if ok else "OFF")