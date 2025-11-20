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

logger = logging.getLogger(__name__)

discovery_prefix_default = os.getenv("HA_DISCOVERY_PREFIX", "homeassistant")

class HomeAssistant:

    def __init__(self, machine: StateMachine, topic_prefix: str, device_name: str, discovery_prefix: str = discovery_prefix_default):
        self.discovery_prefix  = discovery_prefix
        self.device_name = device_name
        self.topic_prefix = topic_prefix
        self.machine: StateMachine = machine
        self.vent: Vent = machine.data["vent"]
        self.mqtt_client = machine.data["mqtt_client"]
        self.last_rssi = None
        self.last_vent_position = None

        # Use temporary Vent to determine the number of motor steps from open to closed
        # so we can match this to the range for the Home Assistant Valve integration
        temp_vent = create_vent_from_env()
        temp_vent.update_from_hardware(temp_vent.open_position) # Fully open the valve
        steps, angle_delta, revs = temp_vent.close(1.0) # Get steps to fully close
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
                    "unit_of_measurement": "dBm",
                    "ent_cat": "diagnostic",
                    "unique_id": f"{self.device_name}_rssi",
                    "state_topic": f"{self.topic_prefix}/rssi/state",
                    "avty": [
                        { "topic": f"{self.topic_prefix}/status"},
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
                        { "topic": f"{self.topic_prefix}/status"},
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
                        { "topic": f"{self.topic_prefix}/status"},
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
                        { "topic": f"{self.topic_prefix}/status"},
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
                        { "topic": f"{self.topic_prefix}/status"},
                    ],
                }
            },
            "qos": 0
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
            vent_position = 1.0 - float(message)/self.position_open
            vent_position = max(0.0, min(vent_position, 1.0))
            
            if self.machine.handle_move_request(vent_position):
                logger.info("Set vent request accepted: vent_position=%.3f", vent_position)
            else:
                logger.warning("Set vent request rejected by state %s", self.machine.current_state)
                
        except ValueError as ve:
            logger.error("set_air_vent: %s", ve)

    def get_command_handlers(self) -> dict[str, Callable[[str], None]]:
        "Provide the callbacks for MQTT command_topics"
        return {
            f"{self.topic_prefix}/air_vent/set": self.set_air_vent,
            f"{self.discovery_prefix}/status": self.ha_status,
        }

    def send_encoder_status(self, status_md, status_ml, status_mh):
        """Update encoder_magnet_detected, encoder_magnet_weak, and encoder_magnet_strong components
        """
        state_string = lambda value: "ON" if value else "OFF"
        self.mqtt_client.publish(f"{self.topic_prefix}/encoder_md/state", state_string(status_md))
        self.mqtt_client.publish(f"{self.topic_prefix}/encoder_ml/state", state_string(status_ml))
        self.mqtt_client.publish(f"{self.topic_prefix}/encoder_mh/state", state_string(status_mh))

    def send_rssi(self):
        try:
            rssi = wifi.radio.ap_info.rssi
            if self.last_rssi != rssi:
                self.mqtt_client.publish(f"{self.topic_prefix}/rssi/state", str(rssi))
                self.last_rssi = rssi
        except Exception as e:
            logger.error("send_rssi: %s", e)

    def update(self):
        "Update HA entities"
        self.send_rssi()
        vent_position = self.vent.get_position()
        ha_vent = str(round(self.position_open * (1 - vent_position)))
        if self.last_vent_position != ha_vent:
            self.mqtt_client.publish(f"{self.topic_prefix}/air_vent/state", ha_vent)
            self.last_vent_position = ha_vent
