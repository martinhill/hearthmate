#!/usr/bin/env python3
"""
StoveLink MQTT Service

A standalone service that listens to MQTT topics for StoveLink thermal camera packets,
decodes them, stores data in HDF5 format, generates RGB images, and publishes them
to Home Assistant MQTT camera topics.

This relieves the MCU from image generation processing.

HDF5 files are automatically rotated when a combustion time reset is detected (indicating
a new firing cycle). Completed files are organized in YYYYMM/DD subdirectories with
filenames in the format YYYYMMDD-NN.h5 where NN is the cycle number.

Usage:
    python3 stovelink_service.py --mqtt-host localhost --input-topic stovelink/data --output-topic homeassistant/camera/thermal/image --hdf5-dir /data/thermal

Dependencies:
    - paho-mqtt: MQTT client
    - h5py: HDF5 file handling
    - numpy: High-performance array operations
"""

import argparse
import base64
import json
import logging
import shutil
import struct
from datetime import datetime
from pathlib import Path
from typing import Optional

import h5py
import numpy as np
import paho.mqtt.client as mqtt

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


class StoveLinkDecoder:
    """
    Decoder for StoveLink binary protocol packets.

    Decodes thermal camera frames with metadata from the custom binary format.
    """

    PACKET_SIZE = 1552  # 16 byte header + 1536 byte body
    HEADER_SIZE = 16
    THERMAL_PIXELS = 768  # 32x24

    def decode_packet(self, packet: bytes) -> dict:
        """
        Decode a StoveLink binary packet.

        Args:
            packet: Binary packet data (1552 bytes)

        Returns:
            dict: Decoded packet with keys:
                - sequence_id: Frame sequence number (uint32)
                - timestamp_ms: Milliseconds since boot (uint32)
                - vent_position: Vent position 0-100% (float)
                - combustion_time: Seconds since burn start (uint16)
                - thermal_frame: Numpy array of 768 temperatures in Celsius (float)

        Raises:
            ValueError: If packet size is incorrect
        """
        if len(packet) != self.PACKET_SIZE:
            raise ValueError(
                f"Invalid packet size: {len(packet)} bytes (expected {self.PACKET_SIZE})"
            )

        # Decode header (16 bytes, little-endian)
        header = packet[: self.HEADER_SIZE]
        seq_id, timestamp, vent_pct, comb_time, reserved = struct.unpack("<IIfHH", header)

        # Decode thermal frame (1536 bytes = 768 x uint16)
        body = packet[self.HEADER_SIZE :]
        thermal_uint16 = struct.unpack("<768H", body)

        # Convert thermal data from uint16 (0.1°C units) to float (°C)
        thermal_frame = np.array([temp / 10.0 for temp in thermal_uint16], dtype=np.float32)
        thermal_frame = thermal_frame.reshape(24, 32)

        return {
            "sequence_id": seq_id,
            "timestamp_ms": timestamp,
            "vent_position": round(vent_pct, 1),
            "combustion_time": comb_time,
            "thermal_frame": thermal_frame,
        }


class ThermalImageGenerator:
    """
    Generates RGB images from thermal frames using ironbow colormap.
    Based on thermal_camera.py implementation but optimized for numpy.
    """

    def __init__(self, width=32, height=24):
        self.width = width
        self.height = height

    def frame_to_rgb(self, frame: np.ndarray, colormap="ironbow") -> np.ndarray:
        """
        Convert thermal frame to RGB data.

        The input thermal frame is encoded from right to left, so we flip it
        horizontally to match BMP image left-to-right encoding.

        Args:
            frame: Thermal frame data (numpy array of temperatures)
            colormap: Color palette to use ('ironbow', 'grayscale')

        Returns:
            np.ndarray: RGB data (height, width, 3) uint8
        """
        # Flip frame horizontally (left-right) to correct sensor encoding
        frame_flipped = np.fliplr(frame)

        min_temp = np.min(frame_flipped)
        max_temp = np.max(frame_flipped)
        temp_range = max_temp - min_temp

        if temp_range < 0.1:
            temp_range = 0.1  # Avoid division by zero

        # Normalize temperature to 0-1 range
        normalized = (frame_flipped - min_temp) / temp_range
        np.clip(normalized, 0.0, 1.0, out=normalized)

        if colormap == "ironbow":
            return self._ironbow_colormap(normalized)
        else:  # grayscale
            gray = (normalized * 255).astype(np.uint8)
            return np.stack([gray, gray, gray], axis=-1)

    def _ironbow_colormap(self, normalized: np.ndarray) -> np.ndarray:
        """
        Apply ironbow colormap to normalized values.

        Args:
            normalized: Normalized temperature values (0-1)

        Returns:
            np.ndarray: RGB values (height, width, 3) uint8
        """
        # Reshape to 2D for processing
        frame_2d = normalized.reshape(self.height, self.width)

        r = np.zeros_like(frame_2d, dtype=np.uint8)
        g = np.zeros_like(frame_2d, dtype=np.uint8)
        b = np.zeros_like(frame_2d, dtype=np.uint8)

        # Ironbow color transitions
        mask1 = frame_2d < 0.25
        ratio1 = frame_2d[mask1] / 0.25
        g[mask1] = (ratio1 * 255).astype(np.uint8)
        b[mask1] = 255

        mask2 = (frame_2d >= 0.25) & (frame_2d < 0.5)
        ratio2 = (frame_2d[mask2] - 0.25) / 0.25
        g[mask2] = 255
        b[mask2] = ((1 - ratio2) * 255).astype(np.uint8)

        mask3 = (frame_2d >= 0.5) & (frame_2d < 0.75)
        ratio3 = (frame_2d[mask3] - 0.5) / 0.25
        r[mask3] = (ratio3 * 255).astype(np.uint8)
        g[mask3] = 255

        mask4 = frame_2d >= 0.75
        ratio4 = (frame_2d[mask4] - 0.75) / 0.25
        r[mask4] = 255
        g[mask4] = ((1 - ratio4) * 255).astype(np.uint8)

        return np.stack([r, g, b], axis=-1)

    def encode_bmp(self, rgb_data: np.ndarray) -> bytes:
        """
        Encode RGB data to BMP format.

        Args:
            rgb_data: RGB pixel data (height, width, 3)

        Returns:
            bytes: BMP file data
        """
        height, width = rgb_data.shape[:2]

        # BMP file header (14 bytes)
        row_size = ((width * 3 + 3) // 4) * 4  # Rows must be padded to 4-byte boundary
        pixel_array_size = row_size * height
        file_size = 54 + pixel_array_size  # 54 = header size

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

        for y in range(height):
            # Flip row order (BMP is bottom-up)
            src_row = height - 1 - y
            dst_offset = y * row_size

            for x in range(width):
                src_idx = (src_row, x)
                dst_idx = dst_offset + (x * 3)

                # Convert RGB to BGR
                pixel_data[dst_idx] = rgb_data[src_idx][2]  # B
                pixel_data[dst_idx + 1] = rgb_data[src_idx][1]  # G
                pixel_data[dst_idx + 2] = rgb_data[src_idx][0]  # R

        return bytes(bmp_header + pixel_data)

    def get_base64_image(self, frame: np.ndarray, colormap="ironbow") -> str:
        """
        Get base64-encoded BMP image data for Home Assistant MQTT camera.

        Args:
            frame: Thermal frame data
            colormap: Color palette to use

        Returns:
            str: Base64-encoded image data
        """
        rgb_data = self.frame_to_rgb(frame, colormap)
        bmp_data = self.encode_bmp(rgb_data)
        return base64.b64encode(bmp_data).decode("ascii")


class HDF5Storage:
    """
    Handles HDF5 storage of thermal camera data with automatic file rotation.
    """

    def __init__(self, base_dir: str, initial_filename: str = "current_cycle.h5"):
        self.base_dir = Path(base_dir)
        # Ensure base directory exists
        self.base_dir.mkdir(parents=True, exist_ok=True)
        # Initial file is in the base directory
        self.current_filename = str(self.base_dir / initial_filename)
        self.file: Optional[h5py.File] = None
        self.last_combustion_time: Optional[int] = None

    def open(self, filename: Optional[str] = None):
        """Open HDF5 file for writing."""
        if filename:
            self.current_filename = filename
        self.file = h5py.File(self.current_filename, "a")

        # Create datasets if they don't exist
        if "mlx90640_frames" not in self.file:
            # Unlimited size datasets
            self.file.create_dataset(
                "mlx90640_frames",
                shape=(0, 24, 32),
                maxshape=(None, 24, 32),
                dtype=np.float32,
                chunks=True,
            )

            self.file.create_dataset(
                "sequence_ids", shape=(0,), maxshape=(None,), dtype=np.uint32, chunks=True
            )

            self.file.create_dataset(
                "timestamps_ms", shape=(0,), maxshape=(None,), dtype=np.uint32, chunks=True
            )

            self.file.create_dataset(
                "vent_positions", shape=(0,), maxshape=(None,), dtype=np.float32, chunks=True
            )

            self.file.create_dataset(
                "combustion_times", shape=(0,), maxshape=(None,), dtype=np.uint16, chunks=True
            )

    def store_packet(self, decoded_packet: dict):
        """Store a decoded packet in HDF5."""
        if self.file is None:
            raise RuntimeError("HDF5 file not opened")

        # Extend datasets
        current_size = self.file["mlx90640_frames"].shape[0]
        new_size = current_size + 1

        for dataset_name in [
            "mlx90640_frames",
            "sequence_ids",
            "timestamps_ms",
            "vent_positions",
            "combustion_times",
        ]:
            self.file[dataset_name].resize(new_size, axis=0)

        # Store data
        self.file["mlx90640_frames"][current_size] = decoded_packet["thermal_frame"]
        self.file["sequence_ids"][current_size] = decoded_packet["sequence_id"]
        self.file["timestamps_ms"][current_size] = decoded_packet["timestamp_ms"]
        self.file["vent_positions"][current_size] = decoded_packet["vent_position"]
        self.file["combustion_times"][current_size] = decoded_packet["combustion_time"]

        # Flush to disk
        self.file.flush()

    def close(self):
        """Close HDF5 file."""
        if self.file is not None:
            self.file.close()
            self.file = None

    def _get_next_cycle_number(self, date_dir: Path) -> int:
        """
        Find the next available cycle number for a given date.

        Args:
            date_dir: Directory path containing cycle files (YYYYMM/DD/)

        Returns:
            int: Next cycle number (starting from 1)
        """
        if not date_dir.exists():
            return 1

        # Find existing files matching YYYYMMDD-*.h5 pattern
        date_str = date_dir.parent.name + date_dir.name  # YYYYMMDD
        existing_files = list(date_dir.glob(f"{date_str}-*.h5"))

        if not existing_files:
            return 1

        # Extract cycle numbers from filenames
        max_cycle = 0
        for file_path in existing_files:
            try:
                # Extract NN from YYYYMMDD-NN.h5
                cycle_str = file_path.stem.split("-")[1]
                cycle_num = int(cycle_str)
                max_cycle = max(max_cycle, cycle_num)
            except (IndexError, ValueError):
                continue

        return max_cycle + 1

    def rotate_file(self):
        """
        Close current file and move it to archive directory structure.
        Creates new file for next combustion cycle.
        """
        if self.file is None:
            return

        # Close current file
        self.close()

        # Get current timestamp for directory structure
        now = datetime.now()
        month_dir = self.base_dir / now.strftime("%Y%m")
        day_dir = month_dir / now.strftime("%d")

        # Create directory structure if needed
        day_dir.mkdir(parents=True, exist_ok=True)

        # Get next cycle number
        cycle_num = self._get_next_cycle_number(day_dir)

        # Build new filename: YYYYMMDD-NN.h5
        date_str = now.strftime("%Y%m%d")
        new_filename = day_dir / f"{date_str}-{cycle_num:02d}.h5"

        # Move current file to archive location
        current_path = Path(self.current_filename)
        if current_path.exists():
            shutil.move(str(current_path), str(new_filename))
            logger.info(f"Rotated file: {current_path} -> {new_filename}")

        # Open new file with temporary name in base directory
        temp_filename = self.base_dir / "thermal_data_current.h5"
        self.current_filename = str(temp_filename)
        self.open()

    def check_rotation(self, combustion_time: int) -> bool:
        """
        Check if file should be rotated based on combustion time reset.

        Args:
            combustion_time: Current combustion time in seconds

        Returns:
            bool: True if rotation occurred
        """
        # Detect reset: combustion_time drops significantly from previous value
        if self.last_combustion_time is not None:
            if combustion_time < self.last_combustion_time - 10:  # Allow some jitter
                logger.info(
                    f"Combustion time reset detected: {self.last_combustion_time}s -> {combustion_time}s"
                )
                self.rotate_file()
                self.last_combustion_time = combustion_time
                return True

        self.last_combustion_time = combustion_time
        return False


class StoveLinkService:
    """
    MQTT service for processing StoveLink thermal camera data.
    """

    def __init__(
        self,
        mqtt_host: str,
        mqtt_port: int,
        input_topic: str,
        output_topic: str,
        hdf5_dir: str,
        username: Optional[str] = None,
        password: Optional[str] = None,
        diagnostic_topic: Optional[str] = None,
    ):
        self.mqtt_host = mqtt_host
        self.mqtt_port = mqtt_port
        self.input_topic = input_topic
        self.output_topic = output_topic
        self.diagnostic_topic = diagnostic_topic
        self.username = username
        self.password = password

        self.decoder = StoveLinkDecoder()
        self.image_generator = ThermalImageGenerator()
        self.storage = HDF5Storage(hdf5_dir)

        self.mqtt_client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
        if username and password:
            self.mqtt_client.username_pw_set(username, password)

        self.mqtt_client.on_connect = (
            lambda client, userdata, connect_flags, reason_code, properties=None: self._on_connect(
                client, userdata, connect_flags, reason_code, properties
            )
        )
        self.mqtt_client.on_message = (
            lambda client, userdata, message, properties=None: self._on_message(
                client, userdata, message, properties
            )
        )
        self.mqtt_client.on_disconnect = (
            lambda client,
            userdata,
            disconnect_flags,
            reason_code,
            properties=None: self._on_disconnect(
                client, userdata, disconnect_flags, reason_code, properties
            )
        )

        self.packet_count = 0

    def start(self):
        """Start the service."""
        logger.info("Starting StoveLink MQTT service")

        # Open HDF5 storage
        self.storage.open()

        # Connect to MQTT broker
        try:
            self.mqtt_client.connect(self.mqtt_host, self.mqtt_port, 60)
            self.mqtt_client.loop_forever()
        except KeyboardInterrupt:
            logger.info("Shutting down service")
        finally:
            self.storage.close()
            self.mqtt_client.disconnect()

    def _on_connect(self, client, userdata, connect_flags, reason_code, properties=None):
        """MQTT connect callback."""
        if reason_code == 0:
            logger.info(f"Connected to MQTT broker at {self.mqtt_host}:{self.mqtt_port}")
            client.subscribe(self.input_topic)
            logger.info(f"Subscribed to topic: {self.input_topic}")
        else:
            logger.error(f"Failed to connect to MQTT broker: {reason_code}")

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties=None):
        """MQTT disconnect callback."""
        logger.info(f"Disconnected from MQTT broker (reason: {reason_code})")

    def _on_message(self, client, userdata, message, properties=None):
        """MQTT message callback."""
        try:
            # Decode packet
            decoded = self.decoder.decode_packet(message.payload)
            self.packet_count += 1

            logger.info(f"Processed packet #{self.packet_count} (seq: {decoded['sequence_id']})")

            # Check for file rotation (combustion time reset)
            self.storage.check_rotation(decoded["combustion_time"])

            # Store in HDF5
            self.storage.store_packet(decoded)

            # Generate and publish image
            base64_image = self.image_generator.get_base64_image(decoded["thermal_frame"])
            client.publish(self.output_topic, base64_image, qos=1, retain=True)

            if self.diagnostic_topic:
                diagnostics = {
                    key: decoded.get(key)
                    for key in ["sequence_id", "timestamp_ms", "vent_position", "combustion_time"]
                }
                client.publish(self.diagnostic_topic, json.dumps(diagnostics))

            logger.debug(f"Published image to topic: {self.output_topic}")

        except Exception as e:
            logger.error(f"Error processing message: {e}")


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="StoveLink MQTT service for thermal camera data processing",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument("--mqtt-host", default="localhost", help="MQTT broker host")
    parser.add_argument("--mqtt-port", type=int, default=1883, help="MQTT broker port")
    parser.add_argument("--username", help="MQTT username")
    parser.add_argument("--password", help="MQTT password")
    parser.add_argument(
        "--input-topic", required=True, help="MQTT topic to subscribe for StoveLink data"
    )
    parser.add_argument(
        "--output-topic", required=True, help="MQTT topic to publish thermal images"
    )
    parser.add_argument(
        "--hdf5-dir",
        required=True,
        help="Base directory for HDF5 data storage (files will be organized in YYYYMM/DD subdirectories)",
    )
    parser.add_argument(
        "--diagnostic-topic",
        default=None,
        required=False,
        help="MQTT topic to pubish diagnostic info",
    )

    args = parser.parse_args()

    service = StoveLinkService(
        mqtt_host=args.mqtt_host,
        mqtt_port=args.mqtt_port,
        input_topic=args.input_topic,
        output_topic=args.output_topic,
        diagnostic_topic=args.diagnostic_topic,
        hdf5_dir=args.hdf5_dir,
        username=args.username,
        password=args.password,
    )

    service.start()


if __name__ == "__main__":
    main()
