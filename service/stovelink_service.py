#!/usr/bin/env python3
"""
StoveLink MQTT Service

A standalone service that listens to MQTT topics for StoveLink thermal camera packets,
decodes them, stores data in HDF5 format, generates RGB images, and publishes them
to Home Assistant MQTT camera topics.

This relieves the MCU from image generation processing.

HDF5 files are automatically rotated when a combustion time reset is detected (indicating
a new firing cycle), when the burn timer starts counting again after the device sat idle,
or when the burn is over: after MAX_BURN_SECONDS of combustion time, or once a burn older
than TEMP_CHECK_AFTER_SECONDS no longer shows fire in the frame. Between burns and while
the device idles the current file is archived or left alone and nothing is written until
the next firing cycle starts.
Completed files are organized in YYYYMM/DD subdirectories with
filenames in the format YYYYMMDD-NN.h5 where NN is the cycle number.

With --log-topic, the device's MQTT log lines are written to a text sidecar next to the HDF5
file (current_cycle.log, rotated to YYYYMMDD-NN.log with it). Each line is tab separated:
    <received wall time>  <sequence_id>  <combustion_time>  <device log line>
sequence_id and combustion_time are those of the first StoveLink packet received after the log
line, i.e. the line was logged just before that frame. "-" means no packet followed within
the hold time (e.g. camera or device offline).

Usage:
    python3 stovelink_service.py --mqtt-host localhost --input-topic stovelink/data --dynamic-topic homeassistant/camera/thermal/dynamic --static-topic homeassistant/camera/thermal/static --hdf5-dir /data/thermal

Dependencies:
    - paho-mqtt: MQTT client
    - h5py: HDF5 file handling
    - numpy: High-performance array operations
"""

import argparse
import base64
import json
import logging
import os
import re
import shutil
import struct
import time
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

    PACKET_SIZE = 1556  # 16 byte header + 1536 byte body
    HEADER_SIZE = 20
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
        seq_id, timestamp, vent_pct, comb_time, tmp36_temp, reserved = struct.unpack("<IIfHfH", header)

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
            "tmp36_temperature": tmp36_temp,
            "thermal_frame": thermal_frame,
        }


class ThermalImageGenerator:
    """
    Generates RGB images from thermal frames using ironbow colormap.
    Based on thermal_camera.py implementation but optimized for numpy.
    """

    def __init__(self, width=32, height=24, min_temp=None, max_temp=None):
        self.width = width
        self.height = height
        self.fixed_min_temp = min_temp
        self.fixed_max_temp = max_temp

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

        if self.fixed_min_temp is not None and self.fixed_max_temp is not None:
            min_temp = self.fixed_min_temp
            max_temp = self.fixed_max_temp
        else:
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

    The HDF5 file and its log sidecar are opened only while a packet or log lines are written,
    a few milliseconds every 10 s, so other programs can read the current cycle during a burn
    (a file held open on the CIFS share can't even be copied). A write that fails, e.g. while a
    reader has the file open, is kept and retried with the next one.

    A cycle starts when the device restarts its combustion timer (new firing cycle after a
    reload) or when the timer starts counting again after the device sat idle (e.g. after a
    reboot between burns, where the timer sits at zero and no reset is detectable). A cycle
    ends when the burn is over: after MAX_BURN_SECONDS of combustion time, or once a burn
    older than TEMP_CHECK_AFTER_SECONDS no longer shows fire (no pixel above TEMP_THRESHOLD).
    At the end the current file is archived and nothing is written until the next cycle
    starts, so idle days between burns don't pile cold frames into the current file. While
    the device idles (timer at zero across consecutive packets) frames are paused the same
    way; only the live images keep being published.
    """

    # HDF5 dataset name -> decoded packet key
    DATASETS = {
        "mlx90640_frames": "thermal_frame",
        "sequence_ids": "sequence_id",
        "timestamps_ms": "timestamp_ms",
        "vent_positions": "vent_position",
        "tmp36_temps": "tmp36_temperature",
        "combustion_times": "combustion_time",
    }
    # Packets kept while the file can't be written: an hour at one frame per 10 s
    MAX_PENDING = 360
    # No burn lasts longer than this before reducing to ash: past it the cycle is over even
    # if the frames still show fire (the device keeps counting while it sits closed for days)
    MAX_BURN_SECONDS = 6 * 3600
    # The temperature criterion only applies from this combustion time on: a fresh fire may
    # take a while to heat the stove above TEMP_THRESHOLD
    TEMP_CHECK_AFTER_SECONDS = 2 * 3600
    # With no pixel above this temperature a mature burn is down to coals: the rest is idle
    TEMP_THRESHOLD = 100.0  # °C

    def __init__(self, base_dir: str, initial_filename: str = "current_cycle.h5"):
        self.base_dir = Path(base_dir)
        # Ensure base directory exists
        self.base_dir.mkdir(parents=True, exist_ok=True)
        # Initial file is in the base directory
        self.initial_filename = initial_filename
        self.current_filename = str(self.base_dir / initial_filename)
        self.is_open = False
        self.pending: list = []  # packets not yet written
        self.pending_log: list = []  # log lines not yet written
        self.last_combustion_time: Optional[int] = None
        # Device idle (burn timer at zero across consecutive packets): frames are paused
        self.idle = False

    @property
    def log_filename(self) -> str:
        """Device log sidecar next to the current HDF5 file, e.g. current_cycle.log"""
        return str(Path(self.current_filename).with_suffix(".log"))

    @property
    def is_recording(self) -> bool:
        """True while packets of a live burn are being stored: the file is open and the
        device is not sitting idle between burns."""
        return self.is_open and not self.idle

    def append_log(self, lines: list):
        """Append device log lines to the sidecar of the current cycle. Between cycles and
        while the device sits idle nothing is written: that chatter belongs to no burn."""
        if not self.is_recording:
            logger.debug(f"Not recording: dropping {len(lines)} device log line(s)")
            return
        self.pending_log.extend(lines)
        self._write_pending_log()

    def _write_pending_log(self) -> bool:
        if not self.pending_log:
            return True
        try:
            with open(self.log_filename, "a", encoding="utf-8") as log_file:
                for line in self.pending_log:
                    log_file.write(line + "\n")
                log_file.flush()
                os.fsync(log_file.fileno())
        except OSError as e:
            logger.warning(f"Could not write {self.log_filename}, will retry: {e}")
            return False
        self.pending_log = []
        return True

    def open(self, filename: Optional[str] = None):
        """Create the HDF5 file and its datasets if needed, and start storing to it."""
        if filename:
            self.current_filename = filename
        with h5py.File(self.current_filename, "a") as file:
            # Unlimited size datasets
            if "mlx90640_frames" not in file:
                file.create_dataset(
                    "mlx90640_frames",
                    shape=(0, 24, 32),
                    maxshape=(None, 24, 32),
                    dtype=np.float32,
                    chunks=True,
                )
            for name, dtype in (
                ("sequence_ids", np.uint32),
                ("timestamps_ms", np.uint32),
                ("vent_positions", np.float32),
                ("combustion_times", np.uint16),
                ("tmp36_temps", np.float32),
            ):
                if name not in file:
                    file.create_dataset(
                        name, shape=(0,), maxshape=(None,), dtype=dtype, chunks=True
                    )
        self.is_open = True

    def store_packet(self, decoded_packet: dict):
        """Store a decoded packet in HDF5."""
        if not self.is_open:
            raise RuntimeError("HDF5 file not opened")
        self.pending.append(decoded_packet)
        if len(self.pending) > self.MAX_PENDING:
            self.pending.pop(0)
            logger.error("HDF5 file unwritable for too long: dropping the oldest frame")
        self._write_pending()

    def _write_pending(self) -> bool:
        """Append the pending packets; on failure keep them for the next attempt."""
        if not self.pending:
            return True
        try:
            # Closing the file flushes it to the server: on the CIFS share written data otherwise
            # stays in the client cache, and a crash would lose the cycle
            with h5py.File(self.current_filename, "a") as file:
                size = file["mlx90640_frames"].shape[0]
                count = len(self.pending)
                for name in self.DATASETS:
                    file[name].resize(size + count, axis=0)
                for i, packet in enumerate(self.pending):
                    for name, key in self.DATASETS.items():
                        file[name][size + i] = packet[key]
        except OSError as e:
            logger.warning(
                f"Could not write {self.current_filename} ({len(self.pending)} frames "
                f"pending), will retry: {e}"
            )
            return False
        self.pending = []
        return True

    def close(self):
        """Write anything pending and stop storing to the current file."""
        if self.is_open:
            self._write_pending()
        self._write_pending_log()
        self.is_open = False

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

    def rotate_file(self, reopen: bool = True):
        """
        Close current file and move it to archive directory structure.

        Args:
            reopen: Open a new file for the next combustion cycle. Without it the storage
            stays closed (end of cycle): nothing is written until the next cycle starts.
        """
        if not self.is_open:
            return

        # Write what is pending and stop storing to the current file
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

        # Move current file and its log sidecar to archive location
        current_path = Path(self.current_filename)
        if current_path.exists():
            shutil.move(str(current_path), str(new_filename))
            logger.info(f"Rotated file: {current_path} -> {new_filename}")
        log_path = Path(self.log_filename)
        if log_path.exists():
            shutil.move(str(log_path), str(new_filename.with_suffix(".log")))

        # Next file uses the temporary name in base directory
        self.current_filename = str(self.base_dir / self.initial_filename)
        if reopen:
            self.open()
        else:
            logger.info(
                "Burn cycle ended: nothing is written until the next combustion time reset"
            )

    def end_cycle(self):
        """Archive the current file and stop writing until the next cycle starts."""
        self.rotate_file(reopen=False)

    def check_rotation(self, combustion_time: int) -> bool:
        """
        Check if a new cycle starts, based on the device's burn timer.

        The device restarts its combustion timer when a firing cycle begins after the vent
        was reloaded while closed. It also starts counting from zero after the device sat
        idle (e.g. a watchdog reboot between burns), where no drop is detectable. On either
        the current file is archived; after an ended cycle, writing resumes with a new file.

        Args:
            combustion_time: Current combustion time in seconds

        Returns:
            bool: True if a new cycle started
        """
        started = False
        was_idle = self.idle
        prev = self.last_combustion_time
        # Detect reset: combustion_time drops significantly from previous value
        if prev is not None and combustion_time < prev - 10:  # Allow some jitter
            logger.info(
                f"Combustion time reset detected: {prev}s -> "
                f"{combustion_time}s, starting a new cycle"
            )
            if self.is_open:
                self.rotate_file()
            else:
                self.open()
            started = True
        elif was_idle and combustion_time > 0:
            # The burn timer started counting after an idle period: new firing cycle
            logger.info(
                f"Combustion time counting again after idle ({combustion_time}s), "
                "starting a new cycle"
            )
            if self.is_open:
                self.rotate_file()
            else:
                self.open()
            started = True
        self.last_combustion_time = combustion_time
        # The device idles with its burn timer at zero; a single zero packet also happens
        # at the very start of a burn, so idleness needs two in a row
        self.idle = combustion_time == 0 and prev == 0
        return started

    def check_end_of_burn(self, combustion_time: int, thermal_frame: np.ndarray) -> bool:
        """
        Check if the burn is over and end the cycle if it is.

        A burn is over after MAX_BURN_SECONDS of combustion time, or once it is older than
        TEMP_CHECK_AFTER_SECONDS and shows no pixel above TEMP_THRESHOLD (down to coals).
        Ending the cycle archives the current file and stops writing until the next
        combustion time reset.

        Args:
            combustion_time: Current combustion time in seconds
            thermal_frame: Decoded thermal frame in Celsius

        Returns:
            bool: True if the cycle ended
        """
        if not self.is_open:
            return False
        if combustion_time >= self.MAX_BURN_SECONDS:
            logger.info(
                f"Combustion time {combustion_time}s over the {self.MAX_BURN_SECONDS}s limit: "
                "no burn lasts that long, ending the cycle"
            )
        elif (
            combustion_time >= self.TEMP_CHECK_AFTER_SECONDS
            and float(np.max(thermal_frame)) < self.TEMP_THRESHOLD
        ):
            logger.info(
                f"Combustion time {combustion_time}s with no pixel above "
                f"{self.TEMP_THRESHOLD}°C: burn is down to coals, ending the cycle"
            )
        else:
            return False
        self.end_cycle()
        return True


# Device log lines that only repeat what each HDF5 frame already records
DEFAULT_LOG_EXCLUDE = r"stovelink: StoveLink packet:"


class StoveLinkService:
    """
    MQTT service for processing StoveLink thermal camera data.
    """

    def __init__(
        self,
        mqtt_host: str,
        mqtt_port: int,
        input_topic: str,
        dynamic_topic: str,
        static_topic: str,
        static_min_temp: float,
        static_max_temp: float,
        hdf5_dir: str,
        username: Optional[str] = None,
        password: Optional[str] = None,
        diagnostic_topic: Optional[str] = None,
        log_topic: Optional[str] = None,
        log_exclude: Optional[str] = DEFAULT_LOG_EXCLUDE,
        log_hold_seconds: float = 30.0,
    ):
        self.mqtt_host = mqtt_host
        self.mqtt_port = mqtt_port
        self.input_topic = input_topic
        self.dynamic_topic = dynamic_topic
        self.static_topic = static_topic
        self.diagnostic_topic = diagnostic_topic
        self.log_topic = log_topic
        self.log_exclude = re.compile(log_exclude) if log_exclude else None
        self.log_hold_seconds = log_hold_seconds
        # Device log lines waiting for the next packet: (monotonic time, wall time, text)
        self.pending_log = []
        self.username = username
        self.password = password

        self.decoder = StoveLinkDecoder()
        self.dynamic_image_generator = ThermalImageGenerator()
        self.static_image_generator = ThermalImageGenerator(
            min_temp=static_min_temp, max_temp=static_max_temp
        )
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
            self._flush_log()
            self.storage.close()
            self.mqtt_client.disconnect()

    def _on_connect(self, client, userdata, connect_flags, reason_code, properties=None):
        """MQTT connect callback."""
        if reason_code == 0:
            logger.info(f"Connected to MQTT broker at {self.mqtt_host}:{self.mqtt_port}")
            client.subscribe(self.input_topic)
            logger.info(f"Subscribed to topic: {self.input_topic}")
            if self.log_topic:
                client.subscribe(self.log_topic)
                logger.info(f"Subscribed to log topic: {self.log_topic}")
        else:
            logger.error(f"Failed to connect to MQTT broker: {reason_code}")

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties=None):
        """MQTT disconnect callback."""
        logger.info(f"Disconnected from MQTT broker (reason: {reason_code})")

    def _on_message(self, client, userdata, message, properties=None):
        """MQTT message callback."""
        if self.log_topic and message.topic == self.log_topic:
            self._on_log_message(message.payload)
            return
        try:
            # Decode packet
            decoded = self.decoder.decode_packet(message.payload)
            self.packet_count += 1

            logger.info(f"Processed packet #{self.packet_count} (seq: {decoded['sequence_id']})")

            # Idle is judged before the cycle state updates: the burn timer at zero for two
            # consecutive packets means the device sits between burns
            idle = (
                decoded["combustion_time"] == 0
                and self.storage.last_combustion_time == 0
            )

            # A combustion time reset - or the timer counting again after idle - starts the
            # next cycle: archives the current file, or reopens writing after an ended cycle
            self.storage.check_rotation(decoded["combustion_time"])

            if self.storage.is_recording:
                # Store in HDF5, then file the log lines that preceded this frame with its
                # cycle
                self.storage.store_packet(decoded)
                self._flush_log(decoded)
                # A burn that is over ends the cycle: archive the file and stop writing until
                # the next combustion time reset. The final frame is stored first, so the
                # archive records the state the burn ended in.
                self.storage.check_end_of_burn(
                    decoded["combustion_time"], decoded["thermal_frame"]
                )
            elif self.storage.is_open:
                # The device idles between burns: frames are paused, and held log lines wait
                # for the next recorded packet so the reload still files with the cycle it
                # precedes
                pass
            else:
                # Between cycles: held log lines belong to no cycle, drop them
                self._flush_log(decoded)

            # Generate and publish dynamic range image
            dynamic_base64_image = self.dynamic_image_generator.get_base64_image(
                decoded["thermal_frame"]
            )
            client.publish(self.dynamic_topic, dynamic_base64_image, qos=0, retain=False)

            # Generate and publish static range image
            static_base64_image = self.static_image_generator.get_base64_image(
                decoded["thermal_frame"]
            )
            client.publish(self.static_topic, static_base64_image, qos=0, retain=False)

            if self.diagnostic_topic:
                diagnostics = {
                    key: decoded.get(key)
                    for key in ["sequence_id", "timestamp_ms", "vent_position", "combustion_time"]
                }
                client.publish(self.diagnostic_topic, json.dumps(diagnostics))

            logger.debug(f"Published images to topics: {self.dynamic_topic}, {self.static_topic}")

        except Exception as e:
            logger.error(f"Error processing message: {e}")


    def _on_log_message(self, payload: bytes):
        """Hold a device log line until the next packet (or the hold time) decides its file."""
        try:
            text = payload.decode("utf-8", errors="replace").rstrip()
            if self.log_exclude and self.log_exclude.search(text):
                return
            received = datetime.now().astimezone().isoformat(timespec="seconds")
            self.pending_log.append((time.monotonic(), received, text))
            # No packet for a while (camera or device offline): don't hold lines forever
            if time.monotonic() - self.pending_log[0][0] > self.log_hold_seconds:
                self._flush_log()
        except Exception as e:
            logger.error(f"Error processing log message: {e}")

    def _flush_log(self, decoded: Optional[dict] = None):
        """Write pending device log lines, stamped with the packet that followed them."""
        if not self.pending_log:
            return
        if decoded is None:
            seq, comb = "-", "-"
        else:
            seq, comb = decoded["sequence_id"], decoded["combustion_time"]
        # Continuation lines (e.g. stack traces) are indented to stay under the text column
        lines = [
            f"{received}\t{seq}\t{comb}\t" + text.replace("\n", "\n\t\t\t")
            for _, received, text in self.pending_log
        ]
        self.pending_log = []
        try:
            self.storage.append_log(lines)
        except Exception as e:
            logger.error(f"Error writing device log: {e}")


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
        "--dynamic-topic", required=True, help="MQTT topic to publish dynamic range thermal images"
    )
    parser.add_argument(
        "--static-topic", required=True, help="MQTT topic to publish static range thermal images"
    )
    parser.add_argument(
        "--static-min-temp",
        type=float,
        default=100.0,
        help="Minimum temperature for static range images (default: 100)",
    )
    parser.add_argument(
        "--static-max-temp",
        type=float,
        default=350.0,
        help="Maximum temperature for static range images (default: 350)",
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

    parser.add_argument(
        "--log-topic",
        default=None,
        help="MQTT topic of the device log (e.g. burnie/log); lines are written to a .log "
        "file next to each HDF5 file",
    )
    parser.add_argument(
        "--log-exclude",
        default=DEFAULT_LOG_EXCLUDE,
        help="Regular expression of device log lines not to write (default: the per-packet "
        "StoveLink echo, which the HDF5 file already holds); empty string keeps all lines",
    )

    args = parser.parse_args()

    service = StoveLinkService(
        mqtt_host=args.mqtt_host,
        mqtt_port=args.mqtt_port,
        input_topic=args.input_topic,
        dynamic_topic=args.dynamic_topic,
        static_topic=args.static_topic,
        static_min_temp=args.static_min_temp,
        static_max_temp=args.static_max_temp,
        diagnostic_topic=args.diagnostic_topic,
        log_topic=args.log_topic,
        log_exclude=args.log_exclude,
        hdf5_dir=args.hdf5_dir,
        username=args.username,
        password=args.password,
    )

    service.start()


if __name__ == "__main__":
    main()
