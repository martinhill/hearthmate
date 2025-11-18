import time
import os
import wifi
import socketpool
import adafruit_logging as logging
from adafruit_minimqtt.adafruit_minimqtt import MQTT, MMQTTException

logger = logging.getLogger(__name__)


class WiFiConnectionManager:
    """
    Manages WiFi connection state with reconnection recovery strategy.
    Implements exponential backoff for reconnection attempts.
    """

    def __init__(self, ssid=None, password=None, base_delay=2, max_delay=300):
        """
        Initialize WiFi connection manager.

        Args:
            ssid: WiFi SSID (defaults to WIFI_SSID env var)
            password: WiFi password (defaults to WIFI_PASSWORD env var)
            base_delay: Initial backoff delay in seconds (default: 2)
            max_delay: Maximum backoff delay in seconds (default: 300)
        """
        self.ssid = ssid or os.getenv("WIFI_SSID")
        self.password = password or os.getenv("WIFI_PASSWORD")
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.current_delay = base_delay
        self.last_reconnect_attempt = 0
        self.connection_failures = 0

    def is_connected(self):
        """Check if WiFi is currently connected"""
        return wifi.radio.connected

    def check_and_recover(self, current_time):
        """
        Check WiFi status and attempt recovery if disconnected.
        Uses exponential backoff to avoid hammering the network.

        Args:
            current_time: Current time from time.monotonic() (for testing)

        Returns:
            bool: True if connected, False otherwise
        """
        if self.is_connected():
            # Reset counters on successful connection
            if self.connection_failures > 0:
                logger.info("WiFi reconnected after %d failures", self.connection_failures)
                self.connection_failures = 0
                self.current_delay = self.base_delay
            return True

        # Not connected - check if backoff period has elapsed
        if current_time - self.last_reconnect_attempt < self.current_delay:
            return False

        self.last_reconnect_attempt = current_time
        self.connection_failures += 1

        try:
            logger.warning(
                "WiFi disconnected (failure #%d). Attempting reconnect (delay: %ds)",
                self.connection_failures,
                self.current_delay,
            )
            wifi.radio.connect(self.ssid, self.password)
            logger.info("WiFi reconnected successfully")
            self.current_delay = self.base_delay
            self.connection_failures = 0
            return True

        except (OSError, RuntimeError, Exception) as e:
            logger.error(
                "WiFi reconnect failed (attempt %d): %s. Retry in %ds",
                self.connection_failures,
                e,
                self.current_delay,
            )
            # Exponential backoff
            self.current_delay = min(self.current_delay * 2, self.max_delay)
            return False

    def get_socket_pool(self):
        """Get a socket pool if WiFi is connected"""
        if self.is_connected():
            return socketpool.SocketPool(wifi.radio)
        return None


class MQTTConnectionManager:
    """
    Manages MQTT reconnection with WiFi health check.
    Only attempts MQTT reconnect if WiFi is stable.
    """

    def __init__(self, mqtt_client, wifi_manager, base_delay=2, max_delay=300):
        """
        Initialize MQTT connection manager.

        Args:
            mqtt_client: MQTT client instance
            wifi_manager: WiFiConnectionManager instance
            base_delay: Initial backoff delay in seconds (default: 2)
            max_delay: Maximum backoff delay in seconds (default: 300)
        """
        self.mqtt_client = mqtt_client
        self.wifi_manager = wifi_manager
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.current_delay = base_delay
        self.last_reconnect_attempt = 0
        self.mqtt_failures = 0

    def attempt_reconnect(self, current_time):
        """
        Attempt MQTT reconnection only if WiFi is stable.

        Args:
            current_time: Current time from time.monotonic() (for testing)

        Returns:
            bool: True if MQTT is connected, False otherwise
        """
        # First check WiFi health
        if not self.wifi_manager.is_connected():
            logger.info("Cannot reconnect MQTT - WiFi is disconnected")
            return False

        # WiFi is OK, check if MQTT is connected
        if self.mqtt_client.is_connected():
            # Reset counters
            if self.mqtt_failures > 0:
                logger.info("MQTT reconnected after %d failures", self.mqtt_failures)
                self.mqtt_failures = 0
                self.current_delay = self.base_delay
            return True

        # Check backoff period
        if current_time - self.last_reconnect_attempt < self.current_delay:
            return False

        self.last_reconnect_attempt = current_time
        self.mqtt_failures += 1

        try:
            logger.info(
                "Attempting MQTT reconnect (failure #%d, delay: %ds)",
                self.mqtt_failures,
                self.current_delay,
            )
            self.mqtt_client.reconnect()
            logger.info("MQTT reconnected successfully")
            self.current_delay = self.base_delay
            self.mqtt_failures = 0
            return True

        except (MMQTTException, OSError, Exception) as e:
            logger.error(
                "MQTT reconnect failed (attempt %d): %s. Retry in %ds",
                self.mqtt_failures,
                e,
                self.current_delay,
            )
            self.current_delay = min(self.current_delay * 2, self.max_delay)
            return False
