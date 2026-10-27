import time
import adafruit_logging as logging

logger = logging.getLogger(__name__)


class MeasurementBuffer:
    """
    Buffer measurements and publish averages at configurable intervals.

    Reduces noise and MQTT message frequency by collecting measurements
    over a time interval and publishing averaged values.
    """

    def __init__(self, interval_seconds=15):
        """
        Initialize measurement buffer.

        Args:
            interval_seconds: Minimum time between publishing averaged values (default 15)
        """
        self.interval_seconds = interval_seconds
        self.buffers = {}
        self.last_publish_time = {}

    def add_measurement(self, name, value):
        """
        Add a measurement to the buffer.

        Args:
            name: Name/key of the measurement (e.g. 'rssi', 'tmp36')
            value: Numeric value to add to buffer
        """
        if name not in self.buffers:
            self.buffers[name] = []
            self.last_publish_time[name] = time.monotonic()

        try:
            # Convert to float to handle any numeric type
            self.buffers[name].append(float(value))
        except (ValueError, TypeError) as e:
            logger.warning("Failed to add measurement %s: %s", name, e)

    def should_publish(self, name):
        """
        Check if enough time has passed to publish averaged value.

        Args:
            name: Name/key of the measurement

        Returns:
            bool: True if interval has passed and buffer has data
        """
        if name not in self.buffers or not self.buffers[name]:
            return False

        current_time = time.monotonic()
        time_elapsed = current_time - self.last_publish_time[name]
        return time_elapsed >= self.interval_seconds

    def get_average(self, name):
        """
        Calculate and return the average of buffered values.

        Args:
            name: Name/key of the measurement

        Returns:
            float: Average of all buffered values, or None if no data
        """
        if name not in self.buffers or not self.buffers[name]:
            return None

        values = self.buffers[name]
        average = sum(values) / len(values)
        return average

    def publish(self, name):
        """
        Get average and reset buffer for a measurement.

        Call this after getting average and successfully publishing the value.

        Args:
            name: Name/key of the measurement

        Returns:
            float: Average of buffered values, or None if no data
        """
        average = self.get_average(name)
        if average is not None:
            self.buffers[name] = []
            self.last_publish_time[name] = time.monotonic()
            logger.debug("Published average for %s: %.2f", name, average)
        return average

    def get_buffer_stats(self, name):
        """
        Get statistics about the buffer for a measurement.

        Args:
            name: Name/key of the measurement

        Returns:
            dict: Contains 'count' (number of buffered values) and 'time_until_publish'
        """
        if name not in self.buffers:
            return {"count": 0, "time_until_publish": self.interval_seconds}

        current_time = time.monotonic()
        time_elapsed = current_time - self.last_publish_time[name]
        time_until_publish = max(0, self.interval_seconds - time_elapsed)

        return {
            "count": len(self.buffers[name]),
            "time_until_publish": time_until_publish,
        }
