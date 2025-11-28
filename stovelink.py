import struct
import time
import adafruit_logging as logging

try:
    from ulab import numpy as np
except ImportError:
    np = None

try:
    from typing import List, Optional, Union
except ImportError:
    pass

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


class StoveLinkEncoder:
    """
    Encoder for the StoveLink binary protocol.
    
    Encodes thermal camera frames with metadata into a custom binary format
    for machine learning data collection and analysis.
    
    Packet Structure (Little-Endian):
    - Header (16 bytes):
        - Sequence ID (uint32, 4 bytes): Rolling counter for frame continuity
        - Timestamp (uint32, 4 bytes): Milliseconds since boot
        - Vent Position (float32, 4 bytes): 0.0 (closed) to 100.0 (open)
        - Combustion Time (uint16, 2 bytes): Seconds since burn cycle start
        - Reserved (uint16, 2 bytes): Padding for alignment
    - Body (1536 bytes):
        - Thermal Frame (768 x uint16): Temperature data in 0.1°C units
    
    Total packet size: 1552 bytes
    """
    
    def __init__(self):
        """Initialize StoveLink encoder with sequence counter."""
        self.sequence_id = 0
        self.boot_time = time.monotonic()
        
    def encode_packet(
        self, 
        thermal_frame, 
        vent_position: float,
        combustion_time: int = 0
    ) -> bytes:
        """
        Encode thermal frame and metadata into StoveLink binary format.
        
        Args:
            thermal_frame: List or numpy array of 768 temperature values in Celsius (32x24 pixels)
            vent_position: Vent position from 0.0 (fully open) to 1.0 (fully closed)
            combustion_time: Seconds since burn cycle started (default: 0)
            
        Returns:
            bytes: Binary packet (1552 bytes) ready for MQTT transmission
            
        Raises:
            ValueError: If thermal_frame doesn't contain exactly 768 values
        """
        if len(thermal_frame) != 768:
            raise ValueError(f"Thermal frame must contain 768 values, got {len(thermal_frame)}")
        
        # Convert vent position from 0.0-1.0 (open-closed) to 0.0-100.0 (closed-open)
        # Invert: 0.0 open becomes 100.0, 1.0 closed becomes 0.0
        vent_position_percent = (1.0 - vent_position) * 100.0
        vent_position_percent = max(0.0, min(vent_position_percent, 100.0))
        
        # Calculate timestamp in milliseconds since boot
        timestamp_ms = int((time.monotonic() - self.boot_time) * 1000)
        
        # Clamp combustion_time to uint16 range (0-65535 seconds)
        combustion_time = max(0, min(combustion_time, 65535))
        
        # Build header (16 bytes) - Little-Endian format
        # Format: <IIfHH = uint32, uint32, float32, uint16, uint16
        header = struct.pack(
            '<IIfHH',
            self.sequence_id,       # Sequence ID (uint32)
            timestamp_ms,            # Timestamp (uint32)
            vent_position_percent,   # Vent Position (float32)
            combustion_time,         # Combustion Time (uint16)
            0                        # Reserved/padding (uint16)
        )
        
        # Build body - convert temperatures to uint16 (multiply by 10)
        # Use vectorized operations if numpy array available, otherwise iterate
        # Format: <768H = 768 x uint16
        if np is not None and type(thermal_frame) == np.ndarray:
            # Use numpy for efficient vectorized conversion
            # Multiply by 10, convert to int, and clamp to uint16 range
            thermal_uint16 = thermal_frame * 10.0
            # Clamp values to uint16 range (0-65535)
            thermal_uint16 = np.clip(thermal_uint16, 0, 65535)
            # Convert to uint16 dtype - ulab doesn't have astype, use array constructor
            thermal_uint16 = np.array(thermal_uint16, dtype=np.uint16)
            # Use tobytes() for direct binary conversion (little-endian on ESP32)
            body = thermal_uint16.tobytes()
        else:
            # Fallback to list iteration
            thermal_data = []
            for temp_celsius in thermal_frame:
                # Convert to 0.1°C units and clamp to uint16 range
                temp_uint16 = int(temp_celsius * 10)
                temp_uint16 = max(0, min(temp_uint16, 65535))
                thermal_data.append(temp_uint16)
            
            body = struct.pack('<768H', *thermal_data)
        
        # Increment sequence counter (wraps at 2^32)
        self.sequence_id = (self.sequence_id + 1) % 0x100000000
        
        # Combine header and body
        packet = header + body
        
        logger.debug(
            "StoveLink packet: seq=%d, ts=%d, vent=%.1f%%, comb=%ds, size=%d",
            self.sequence_id - 1,  # Log the ID we just sent
            timestamp_ms,
            vent_position_percent,
            combustion_time,
            len(packet)
        )
        
        return packet
    
    def reset_sequence(self):
        """Reset sequence counter to zero (useful for testing)."""
        self.sequence_id = 0
        logger.info("StoveLink sequence counter reset")
    
    def get_packet_size(self) -> int:
        """
        Get the size of a StoveLink packet in bytes.
        
        Returns:
            int: Packet size (1552 bytes)
        """
        return 16 + 1536  # Header + Body
