import time
import os
import board
import busio
import binascii
from ulab import numpy as np
import adafruit_logging as logging

import adafruit_mlx90640

try:
    from typing import Dict, List, Optional, Tuple
except ImportError:
    pass

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Constants for histogram-based median calculation
HIST_MIN_TEMP = -10  # Minimum expected temperature in Celsius
HIST_MAX_TEMP = 300  # Maximum expected temperature in Celsius
HIST_BIN_SIZE = 10  # 0.1°C precision (multiply temp by 10)
HIST_SIZE = (HIST_MAX_TEMP - HIST_MIN_TEMP) * HIST_BIN_SIZE + 1


def encode_bmp(rgb_data, width, height):
    """
    Encode RGB data to BMP format for MQTT transmission.
    BMP is simpler than JPEG and doesn't require compression libraries.

    Args:
        rgb_data: RGB pixel data (width * height * 3 bytes)
        width: Image width in pixels
        height: Image height in pixels

    Returns:
        bytes: BMP file data
    """
    # BMP file header (14 bytes)
    row_size = ((width * 3 + 3) // 4) * 4  # Rows must be padded to 4-byte boundary
    pixel_array_size = row_size * height
    file_size = 54 + pixel_array_size  # 54 = header size

    # Build BMP header
    bmp_header = bytearray(54)

    # File header
    bmp_header[0:2] = b"BM"  # Signature
    bmp_header[2:6] = file_size.to_bytes(4, "little")  # File size
    bmp_header[10:14] = (54).to_bytes(4, "little")  # Pixel data offset

    # DIB header (BITMAPINFOHEADER)
    bmp_header[14:18] = (40).to_bytes(4, "little")  # DIB header size
    bmp_header[18:22] = width.to_bytes(4, "little")  # Width
    bmp_header[22:26] = height.to_bytes(4, "little")  # Height
    bmp_header[26:28] = (1).to_bytes(2, "little")  # Color planes
    bmp_header[28:30] = (24).to_bytes(2, "little")  # Bits per pixel
    bmp_header[34:38] = pixel_array_size.to_bytes(4, "little")  # Image size

    # Build pixel array (BMP stores rows bottom-to-top, BGR format)
    pixel_data = bytearray(pixel_array_size)
    padding = row_size - (width * 3)

    for y in range(height):
        # Flip row order (BMP is bottom-up)
        src_row = height - 1 - y
        dst_offset = y * row_size

        for x in range(width):
            src_idx = (src_row * width + x) * 3
            dst_idx = dst_offset + (x * 3)

            # Convert RGB to BGR
            pixel_data[dst_idx] = rgb_data[src_idx + 2]  # B
            pixel_data[dst_idx + 1] = rgb_data[src_idx + 1]  # G
            pixel_data[dst_idx + 2] = rgb_data[src_idx]  # R

    return bytes(bmp_header + pixel_data)


class ThermalCamera:
    """
    Interface for MLX90640 thermal camera with frame capture and colormap conversion.
    Provides thermal imaging at 24x32 resolution with configurable refresh rate.
    """

    def __init__(self, i2c, refresh_rate=8):
        """
        Initialize MLX90640 thermal camera.

        Args:
            i2c: I2C bus instance
            refresh_rate: Camera refresh rate in Hz (default: 2)
        """
        if adafruit_mlx90640 is None:
            raise ImportError("adafruit_mlx90640 library not available")

        self.i2c = i2c
        self.refresh_rate = refresh_rate
        self._initialize_camera()

        # Frame data
        self.frame = [0] * 768
        self.width = 32
        self.height = 24
        self.last_frame_time = 0
        self.retry_count = 0
        self.max_retries = 5

        # Reusable data structures for statistics calculation
        self._temp_histogram = np.zeros(HIST_SIZE, dtype=np.uint16)
        self._freq_map = {}

        logger.info(
            "MLX90640 initialized (serial: %s)",
            [hex(i) for i in self.mlx.serial_number],
        )

    def _initialize_camera(self):
        """
        Initialize or reinitialize the MLX90640 camera hardware.
        Called during __init__ and when recovering from I2C errors.
        """
        self.mlx = adafruit_mlx90640.MLX90640(self.i2c)

        # Set refresh rate - use library constants
        if self.refresh_rate <= 1:
            self.mlx.refresh_rate = adafruit_mlx90640.RefreshRate.REFRESH_1_HZ
        elif self.refresh_rate <= 2:
            self.mlx.refresh_rate = adafruit_mlx90640.RefreshRate.REFRESH_2_HZ
        elif self.refresh_rate <= 4:
            self.mlx.refresh_rate = adafruit_mlx90640.RefreshRate.REFRESH_4_HZ
        elif self.refresh_rate <= 8:
            self.mlx.refresh_rate = adafruit_mlx90640.RefreshRate.REFRESH_8_HZ
        elif self.refresh_rate <= 16:
            self.mlx.refresh_rate = adafruit_mlx90640.RefreshRate.REFRESH_16_HZ
        elif self.refresh_rate <= 32:
            self.mlx.refresh_rate = adafruit_mlx90640.RefreshRate.REFRESH_32_HZ
        else:
            self.mlx.refresh_rate = adafruit_mlx90640.RefreshRate.REFRESH_64_HZ

    def reinitialize(self):
        """
        Reinitialize the camera connection after I2C communication failure.
        This can recover from OSError 32 (broken pipe) and similar I2C errors.

        Returns:
            bool: True if reinitialization succeeded, False otherwise
        """
        try:
            self._initialize_camera()
            logger.info("MLX90640 reinitialized successfully")
            return True
        except (OSError, RuntimeError, Exception) as e:
            logger.error("Failed to reinitialize MLX90640: %s", e)
            raise

    def capture_frame(self):
        """
        Capture a single thermal frame from the camera.
        Retries on failure up to max_retries times.
        Handles both ValueError (transient frame errors) and OSError (I2C communication failures).

        Returns:
            list: Thermal frame data (768 temperature values in Celsius), or None on failure

        Raises:
            OSError: If I2C communication fails persistently (for recovery manager to handle)
        """
        for attempt in range(self.max_retries):
            try:
                self.mlx.getFrame(self.frame)
                self.last_frame_time = time.monotonic()
                self.retry_count = 0
                return self.frame
            except ValueError as e:
                # Transient frame capture error - retry with short delay
                self.retry_count += 1
                if attempt < self.max_retries - 1:
                    logger.debug(
                        "Frame capture retry %d/%d (ValueError)",
                        attempt + 1,
                        self.max_retries,
                    )
                    time.sleep(0.01)
                else:
                    logger.warning(
                        "Failed to capture frame after %d retries: %s",
                        self.max_retries,
                        e,
                    )
                    return None
            except OSError as e:
                # I2C communication error (e.g., errno 32: broken pipe)
                # Log the error and raise it for the recovery manager to handle
                self.retry_count += 1
                logger.error(
                    "I2C communication error during frame capture (attempt %d/%d): %s (errno=%s)",
                    attempt + 1,
                    self.max_retries,
                    e,
                    getattr(e, "errno", "unknown"),
                )
                # Don't retry OSError - raise immediately for recovery
                raise
        return None
        return None

    def get_temperature_range(self, frame=None):
        """
        Get min and max temperatures from a frame.

        Args:
            frame: Temperature frame data (uses last captured frame if None)

        Returns:
            tuple: (min_temp, max_temp) in Celsius
        """
        if frame is None:
            frame = self.frame
        return (min(frame), max(frame))

    def get_np_frame(self):
        "Get a numpy array version of the current frame"
        return np.array(self.frame, dtype=np.float)

    def get_temperature_statistics(self, frame: np.ndarray[float] = None):
        """Calculate basic statistics on the given frame. The argument is assumed to be a numpy array.
        The current frame will be automatically used if provided frame is None.
        """
        if frame is None:
            npframe = frame
        else:
            npframe = np.array(self.frame, dtype=np.float)

        return {
            "min": np.min(npframe),
            "max": np.max(npframe),
            "mean": np.mean(npframe),
            "median": np.median(npframe),
        }

    def frame_to_rgb(self, frame=None, colormap="ironbow"):
        """
        Convert thermal frame to RGB data for image encoding.

        Args:
            frame: Temperature frame data (uses last captured frame if None)
            colormap: Color palette to use ('ironbow', 'grayscale')

        Returns:
            bytearray: RGB data (width * height * 3 bytes)
        """
        if frame is None:
            frame = self.frame

        min_temp, max_temp = self.get_temperature_range(frame)
        temp_range = max_temp - min_temp

        if temp_range < 0.1:
            temp_range = 0.1  # Avoid division by zero

        rgb_data = bytearray(self.width * self.height * 3)

        for i, temp in enumerate(frame):
            # Normalize temperature to 0-1 range
            normalized = (temp - min_temp) / temp_range
            normalized = max(0.0, min(1.0, normalized))

            if colormap == "ironbow":
                # Ironbow colormap: blue -> cyan -> green -> yellow -> red
                r, g, b = self._ironbow_color(normalized)
            else:  # grayscale
                val = int(normalized * 255)
                r, g, b = val, val, val

            idx = i * 3
            rgb_data[idx] = r
            rgb_data[idx + 1] = g
            rgb_data[idx + 2] = b

        return rgb_data

    def _ironbow_color(self, value):
        """
        Convert normalized value (0-1) to ironbow colormap RGB.

        Args:
            value: Normalized temperature value (0.0 to 1.0)

        Returns:
            tuple: (r, g, b) values (0-255)
        """
        # Ironbow color transitions
        if value < 0.25:
            # Blue to cyan
            ratio = value / 0.25
            r = 0
            g = int(ratio * 255)
            b = 255
        elif value < 0.5:
            # Cyan to green
            ratio = (value - 0.25) / 0.25
            r = 0
            g = 255
            b = int((1 - ratio) * 255)
        elif value < 0.75:
            # Green to yellow
            ratio = (value - 0.5) / 0.25
            r = int(ratio * 255)
            g = 255
            b = 0
        else:
            # Yellow to red
            ratio = (value - 0.75) / 0.25
            r = 255
            g = int((1 - ratio) * 255)
            b = 0

        return (r, g, b)

    def get_image_data(self, frame=None, colormap="ironbow", format="bmp"):
        """
        Get encoded image data from thermal frame.

        Args:
            frame: Temperature frame data (uses last captured frame if None)
            colormap: Color palette to use ('ironbow', 'grayscale')
            format: Image format ('bmp' only supported currently)

        Returns:
            bytes: Encoded image data ready for MQTT transmission
        """
        rgb_data = self.frame_to_rgb(frame, colormap)

        if format == "bmp":
            return encode_bmp(rgb_data, self.width, self.height)
        else:
            raise ValueError(f"Unsupported image format: {format}")

    def get_base64_image(self, frame=None, colormap="ironbow", format="bmp"):
        """
        Get base64-encoded image data for Home Assistant MQTT camera.

        Args:
            frame: Temperature frame data (uses last captured frame if None)
            colormap: Color palette to use ('ironbow', 'grayscale')
            format: Image format ('bmp' only supported currently)

        Returns:
            str: Base64-encoded image data
        """
        image_data = self.get_image_data(frame, colormap, format)
        return binascii.b2a_base64(image_data).decode("ascii").strip()


class MockThermalCamera(ThermalCamera):
    """
    Mock implementation of ThermalCamera for testing without physical hardware.
    Generates simulated thermal data with a hot spot pattern.
    """

    def __init__(self, i2c=None, refresh_rate=2):
        """
        Initialize mock thermal camera.

        Args:
            i2c: I2C bus instance (unused, for compatibility)
            refresh_rate: Simulated refresh rate in Hz (default: 2)
        """
        self.width = 32
        self.height = 24
        self.frame = [0] * 768
        self.last_frame_time = 0
        self.retry_count = 0
        self.base_temp = 20.0  # Base ambient temperature
        self.hotspot_temp = 35.0  # Hot spot temperature
        self.time_offset = 0

        logger.info("MockThermalCamera initialized (simulated)")

    def capture_frame(self):
        """
        Generate a simulated thermal frame with animated hot spot.

        Returns:
            list: Simulated thermal frame data (768 temperature values)
        """
        self.last_frame_time = time.monotonic()
        self.time_offset += 0.1

        # Generate frame with moving hot spot
        center_x = 16 + int(8 * (time.monotonic() % 4 - 2))
        center_y = 12 + int(6 * ((time.monotonic() * 0.7) % 4 - 2))

        for h in range(self.height):
            for w in range(self.width):
                idx = h * self.width + w

                # Calculate distance from hot spot center
                dx = w - center_x
                dy = h - center_y
                distance = (dx * dx + dy * dy) ** 0.5

                # Temperature falls off with distance
                if distance < 5:
                    temp = self.hotspot_temp - (distance * 2)
                else:
                    temp = self.base_temp + (1 / (1 + distance * 0.1))

                self.frame[idx] = temp

        return self.frame

    def get_temperature_range(self, frame=None):
        """Get min and max temperatures from frame."""
        if frame is None:
            frame = self.frame
        return (min(frame), max(frame))

    def get_image_data(self, frame=None, colormap="ironbow", format="bmp"):
        """Get encoded image data from thermal frame."""
        rgb_data = self.frame_to_rgb(frame, colormap)

        if format == "bmp":
            return encode_bmp(rgb_data, self.width, self.height)
        else:
            raise ValueError(f"Unsupported image format: {format}")

    def get_base64_image(self, frame=None, colormap="ironbow", format="bmp"):
        """Get base64-encoded image data for Home Assistant MQTT camera."""
        image_data = self.get_image_data(frame, colormap, format)
        return binascii.b2a_base64(image_data).decode("ascii").strip()


def get_thermal_camera(i2c=None, allow_mock=True):
    """
    Get a thermal camera instance based on hardware availability.

    Args:
        i2c: I2C bus instance (creates default if None)

    Returns:
        ThermalCamera or MockThermalCamera instance
    """
    if i2c is None:
        i2c = board.I2C()

    # Scan for MLX90640 on I2C bus
    i2c.try_lock()
    scan = i2c.scan()
    i2c.unlock()

    mlx90640_addr = 0x33  # Default I2C address for MLX90640

    if mlx90640_addr in scan and adafruit_mlx90640 is not None:
        try:
            logger.info("MLX90640 detected at 0x%X", mlx90640_addr)
            return ThermalCamera(i2c)
        except Exception as e:
            logger.error("Failed to initialize MLX90640: %s", e)
            raise
            logger.info("Falling back to MockThermalCamera")
            return MockThermalCamera(i2c)
    elif allow_mock:
        if mlx90640_addr not in scan:
            logger.info("MLX90640 not detected on I2C bus - using mock camera")
        else:
            logger.info("MLX90640 library not available - using mock camera")
        raise RuntimeError("MLX90640 missing")
        return MockThermalCamera(i2c)
