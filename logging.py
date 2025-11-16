from adafruit_logging import NOTSET, Handler, LogRecord
import adafruit_minimqtt.adafruit_minimqtt as MQTT


class MQTTHandler(Handler):
    """
    Log handler that emits log records as MQTT PUBLISH messages.
    """

    def __init__(self, mqtt_client: MQTT.MQTT, topic: str) -> None:
        """
        Assumes that the MQTT client object is already connected.
        """
        super().__init__()

        self._mqtt_client = mqtt_client
        self._topic = topic

        # To make it work also in CPython.
        self.level = NOTSET

    def emit(self, record: LogRecord) -> None:
        """
        Publish message from the LogRecord to the MQTT broker, if connected.
        """
        try:
            if self._mqtt_client.is_connected():
                self._mqtt_client.publish(self._topic, self.format(record))
        except MQTT.MMQTTException:
            pass

    # To make this work also in CPython's logging.
    def handle(self, record: LogRecord) -> None:
        """
        Handle the log record. Here, it means just emit.
        """
        self.emit(record)

