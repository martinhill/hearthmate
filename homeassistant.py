import os
import json
import wifi
import adafruit_logging as logging
try:
    from typing import Callable
except ImportError:
    pass

from state_machine import StateMachine

logger = logging.getLogger(__name__)

discovery_prefix_default = os.getenv("HA_DISCOVERY_PREFIX", "homeassistant")

class HomeAssistant:

    def __init__(self, machine: StateMachine, topic_prefix: str, device_name: str, discovery_prefix: str = discovery_prefix_default):
        self.discovery_prefix  = discovery_prefix
        self.device_name = device_name
        self.topic_prefix = topic_prefix
        self.machine = machine
        self.mqtt_client = machine.data["mqtt_client"]
        self.last_rssi = 0

    def mqtt_discovery(self) -> dict[str, str]:
        discovery_topic = f"{self.discovery_prefix}/device/{self.device_name}/config"

        discovery_dict = {
            "dev": {
                "ids": self.device_name,
                "name": "Burnie"
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
                    "entity_category": "diagnostic",
                    "unique_id": f"{self.device_name}_rssi",
                    "state_topic": f"{self.topic_prefix}/rssi/state",
                },
                "encoder_magnet_detected": {
                    "name": "AS5600 magnet detected",
                    "p": "binary_sensor",
                    "entity_category": "diagnostic",
                    "unique_id": f"{self.device_name}_encoder_md",
                    "state_topic": f"{self.topic_prefix}/encoder_md/state",
                    "icon": "mdi:magnet",
                },
                "encoder_magnet_weak": {
                    "name": "AS5600 magnet weak",
                    "p": "binary_sensor",
                    "entity_category": "diagnostic",
                    "unique_id": f"{self.device_name}_encoder_ml",
                    "state_topic": f"{self.topic_prefix}/encoder_ml/state",
                    "icon": "mdi:magnet",
                },
                "encoder_magnet_strong": {
                    "name": "AS5600 magnet strong",
                    "p": "binary_sensor",
                    "entity_category": "diagnostic",
                    "unique_id": f"{self.device_name}_encoder_mh",
                    "state_topic": f"{self.topic_prefix}/encoder_mh/state",
                    "icon": "mdi:magnet",
                },
                "air_vent": {
                    "name": "Air Vent",
                    "p": "valve",
                    "unique_id": f"{self.device_name}_air_vent",
                    "reports_position": True,
                    "~": f"{self.topic_prefix}/air_vent",
                    "state_topic": "~/state",
                    "command_topic": "~/set",
                }
            },
            "qos": 0
        }

        discovery_json = json.dumps(discovery_dict)

        return {"topic": discovery_topic, "message": discovery_json}

    def set_air_vent(self, message):
        logger.debug("set_air_vent: %s", message)

    def get_command_handlers(self) -> dict[str, Callable[[str], None]]:
        "Provide the callbacks for MQTT command_topics"
        return {
            f"{self.topic_prefix}/air_vent/set": self.set_air_vent,
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
