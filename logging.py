import os
from adafruit_logging import NOTSET, Handler, LogRecord
import adafruit_minimqtt.adafruit_minimqtt as MQTT


class MQTTHandler(Handler):
    """
    Log handler that emits log records as MQTT PUBLISH messages.
    Can be suspended and resumed to prevent publishing log records during MQTT errors.
    """

    def __init__(self, mqtt_client: MQTT.MQTT, topic: str) -> None:
        """
        Assumes that the MQTT client object is already connected.
        """
        super().__init__()

        self._mqtt_client = mqtt_client
        self._topic = topic
        self._suspended = False
        # To make it work also in CPython.
        self.level = NOTSET

    def emit(self, record: LogRecord) -> None:
        """
        Publish message from the LogRecord to the MQTT broker, if connected.
        """
        try:
            if self._mqtt_client.is_connected() and not self._suspended:
                self._mqtt_client.publish(self._topic, self.format(record))
        except MQTT.MMQTTException:
            pass
        except OSError:
            pass

    # To make this work also in CPython's logging.
    def handle(self, record: LogRecord) -> None:
        """
        Handle the log record. Here, it means just emit.
        """
        self.emit(record)
    
    def suspend(self) -> None:
        """
        Suspend the MQTT handler.
        """
        self._suspended = True
    
    def resume(self) -> None:
        """
        Resume the MQTT handler.
        """
        self._suspended = False


class FileHandler(Handler):
    """
    Log handler that writes log records to files in the logs/ directory.
    Uses an incrementing counter for file naming since the MCU RTC resets on power-off.
    """

    def __init__(self, log_dir: str = "logs") -> None:
        """
        Initialize FileHandler with auto-incrementing log file.

        Args:
            log_dir: Directory to store log files (default: "logs")
        """
        super().__init__()
        self._log_dir = log_dir
        self._file = None
        self.level = NOTSET
        self._repeat_data = {}  # key: (logger_name, level), value: {'last_msg': str, 'count': int, 'threshold': int}

        # Create logs directory if it doesn't exist
        try:
            os.mkdir(log_dir)
        except OSError:
            # Directory already exists or other error, continue
            pass

        # Find the next log file number
        self._log_number = self._get_next_log_number()
        self._open_log_file()

    def _get_next_log_number(self) -> int:
        """
        Determine the next log file number by finding the highest existing counter.
        Returns the next incrementing number.
        """
        try:
            files = os.listdir(self._log_dir)
            log_files = [
                f for f in files if f.startswith("log_") and f.endswith(".txt")
            ]

            if not log_files:
                return 0

            # Extract numbers and find the maximum
            numbers = []
            for f in log_files:
                try:
                    num = int(f[4:-4])  # Extract number from "log_XXXX.txt"
                    numbers.append(num)
                except ValueError:
                    continue

            return max(numbers) + 1 if numbers else 0
        except (OSError, ImportError):
            return 0

    def _open_log_file(self) -> None:
        """
        Open a new log file with the current log number.
        """
        try:
            log_path = f"{self._log_dir}/log_{self._log_number:04d}.txt"
            self._file = open(log_path, "a")
        except (OSError, IOError):
            self._file = None

    def emit(self, record: LogRecord) -> None:
        """
        Write the log record to the file, with repeat message detection.
        """
        if self._file is None:
            return

        try:
            key = record.name
            if key not in self._repeat_data:
                self._repeat_data[key] = {"last_msg": None, "levelno": record.levelno, "count": 0, "threshold": 4}
            data = self._repeat_data[key]

            if record.msg == data["last_msg"] and record.levelno == data["levelno"]:
                data["count"] += 1
                if data["count"] >= data["threshold"]:
                    message = self.format(record)
                    self._file.write(f"{message} (repeated {data['count']} times)\n")
                    self._file.flush()
                    data["threshold"] *= 2
                    data["count"] = 0
            else:
                self._file.write(self.format(record) + "\n")
                self._file.flush()
                data["last_msg"] = record.msg
                data["levelno"] = record.levelno
                data["count"] = 0
        except (OSError, IOError):
            pass

    def handle(self, record: LogRecord) -> None:
        """
        Handle the log record by writing it to the file.
        """
        self.emit(record)

    def close(self) -> None:
        """
        Close the log file.
        """
        if self._file is not None:
            try:
                self._file.close()
            except OSError:
                pass
            self._file = None
