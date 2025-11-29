#!/usr/bin/env python3
"""
Test suite for StoveLink service components.
Tests ThermalImageGenerator image flip and encoding.
"""

import sys
import numpy as np
import base64
from io import BytesIO

# Add service directory to path
sys.path.insert(0, "/Users/martin/Hacking/hearthmate/service")

from stovelink_service import ThermalImageGenerator, StoveLinkDecoder
import struct


class TestThermalImageGenerator:
    """Test cases for ThermalImageGenerator"""

    def __init__(self):
        self.generator = ThermalImageGenerator(width=32, height=24)
        self.test_count = 0
        self.passed_count = 0

    def test_frame_to_rgb_shape(self):
        """Test that frame_to_rgb produces correct shape RGB output"""
        print("\n=== Test 1: Frame to RGB Shape ===")
        self.test_count += 1

        # Create a test frame (24x32)
        frame = np.ones((24, 32), dtype=np.float32) * 25.0

        rgb_data = self.generator.frame_to_rgb(frame)

        print(f"Input frame shape: {frame.shape}")
        print(f"Output RGB shape: {rgb_data.shape}")
        print(f"Expected shape: (24, 32, 3)")

        assert rgb_data.shape == (24, 32, 3), f"Shape mismatch: {rgb_data.shape} != (24, 32, 3)"
        assert rgb_data.dtype == np.uint8, f"Dtype should be uint8, got {rgb_data.dtype}"

        print("✓ Frame to RGB shape test passed")
        self.passed_count += 1

    def test_horizontal_flip(self):
        """Test that frame_to_rgb flips the image left-right"""
        print("\n=== Test 2: Horizontal Flip ===")
        self.test_count += 1

        # Create a frame with distinctive left-right pattern
        # Left side: cold (20°C), Right side: hot (80°C)
        frame = np.ones((24, 32), dtype=np.float32)
        frame[:, :16] = 20.0  # Left half: cold
        frame[:, 16:] = 80.0  # Right half: hot

        rgb_data = self.generator.frame_to_rgb(frame)

        # After flipping:
        # - Left side should be hot (was right)
        # - Right side should be cold (was left)

        # In RGB data, hot pixels will have high R value, cold will have high B value
        left_pixels = rgb_data[:, :8, :]  # Leftmost 8 columns
        right_pixels = rgb_data[:, -8:, :]  # Rightmost 8 columns

        left_avg_r = np.mean(left_pixels[:, :, 0])
        right_avg_r = np.mean(right_pixels[:, :, 0])

        left_avg_b = np.mean(left_pixels[:, :, 2])
        right_avg_b = np.mean(right_pixels[:, :, 2])

        print(f"Original: Left=20°C (cold), Right=80°C (hot)")
        print(f"After flip:")
        print(f"  Left pixels: avg R={left_avg_r:.1f}, avg B={left_avg_b:.1f}")
        print(f"  Right pixels: avg R={right_avg_r:.1f}, avg B={right_avg_b:.1f}")
        print(f"Expected: Left should be hot (high R), Right should be cold (high B)")

        # After flip, left should be hotter than right
        # This is verified by R channel being higher on left than right
        assert left_avg_r > right_avg_r, (
            f"Left side R should be higher than right (flip not working): {left_avg_r} vs {right_avg_r}"
        )
        assert left_avg_b < right_avg_b, (
            f"Left side B should be lower than right (flip not working): {left_avg_b} vs {right_avg_b}"
        )

        print("✓ Horizontal flip test passed")
        self.passed_count += 1

    def test_grayscale_colormap(self):
        """Test grayscale colormap output"""
        print("\n=== Test 3: Grayscale Colormap ===")
        self.test_count += 1

        frame = np.array([[10.0, 30.0], [50.0, 70.0]], dtype=np.float32)

        rgb_data = self.generator.frame_to_rgb(frame, colormap="grayscale")

        # In grayscale, R=G=B for each pixel
        print(f"Grayscale output shape: {rgb_data.shape}")

        for i in range(rgb_data.shape[0]):
            for j in range(rgb_data.shape[1]):
                r, g, b = rgb_data[i, j, :]
                assert r == g == b, f"Grayscale mismatch at ({i},{j}): R={r}, G={g}, B={b}"

        print("✓ Grayscale colormap test passed")
        self.passed_count += 1

    def test_ironbow_colormap(self):
        """Test ironbow colormap produces distinct colors"""
        print("\n=== Test 4: Ironbow Colormap ===")
        self.test_count += 1

        # Create frame with temperature gradient (proper 24x32 size)
        frame = np.zeros((24, 32), dtype=np.float32)
        frame[:12, :] = 10.0  # Top half: cold
        frame[12:, :] = 110.0  # Bottom half: hot

        rgb_data = self.generator.frame_to_rgb(frame, colormap="ironbow")

        print(f"Temperature gradient: {frame.min():.1f}°C to {frame.max():.1f}°C")
        print(f"RGB output shape: {rgb_data.shape}")

        # Verify that different temperatures produce different colors
        cold_pixels = rgb_data[:6, :, :]  # Top section: cold
        hot_pixels = rgb_data[-6:, :, :]  # Bottom section: hot

        cold_pixel = np.mean(cold_pixels, axis=(0, 1)).astype(int)
        hot_pixel = np.mean(hot_pixels, axis=(0, 1)).astype(int)

        print(f"Cold pixel avg (10°C): R={cold_pixel[0]}, G={cold_pixel[1]}, B={cold_pixel[2]}")
        print(f"Hot pixel avg (110°C): R={hot_pixel[0]}, G={hot_pixel[1]}, B={hot_pixel[2]}")

        # Cold should have more blue, hot should have more red
        assert cold_pixel[2] > cold_pixel[0], "Cold pixels should be blue"
        assert hot_pixel[0] > hot_pixel[2], "Hot pixels should be red"

        print("✓ Ironbow colormap test passed")
        self.passed_count += 1

    def test_encode_bmp(self):
        """Test BMP encoding produces valid BMP data"""
        print("\n=== Test 5: BMP Encoding ===")
        self.test_count += 1

        # Create simple RGB data
        rgb_data = np.zeros((24, 32, 3), dtype=np.uint8)
        rgb_data[:, :, 0] = 255  # Red channel

        bmp_data = self.generator.encode_bmp(rgb_data)

        print(f"BMP data size: {len(bmp_data)} bytes")
        print(f"First 2 bytes: {bmp_data[:2]}")

        # Check BMP signature
        assert bmp_data[:2] == b"BM", f"BMP signature invalid: {bmp_data[:2]}"

        # Parse file size from header (bytes 2-6, little-endian)
        file_size = struct.unpack("<I", bmp_data[2:6])[0]
        print(f"File size from header: {file_size}")
        assert len(bmp_data) == file_size, f"File size mismatch: {len(bmp_data)} != {file_size}"

        # Parse pixel data offset (bytes 10-14, little-endian)
        pixel_offset = struct.unpack("<I", bmp_data[10:14])[0]
        print(f"Pixel data offset: {pixel_offset}")
        assert pixel_offset == 54, f"Pixel offset should be 54, got {pixel_offset}"

        print("✓ BMP encoding test passed")
        self.passed_count += 1

    def test_base64_encoding(self):
        """Test get_base64_image returns valid base64"""
        print("\n=== Test 6: Base64 Encoding ===")
        self.test_count += 1

        frame = np.ones((24, 32), dtype=np.float32) * 25.0
        base64_image = self.generator.get_base64_image(frame)

        print(f"Base64 string length: {len(base64_image)}")

        # Verify it's valid base64
        try:
            decoded = base64.b64decode(base64_image)
            print(f"Decoded BMP size: {len(decoded)} bytes")
            assert decoded[:2] == b"BM", "Decoded data should be BMP"
            print("✓ Base64 encoding test passed")
            self.passed_count += 1
        except Exception as e:
            print(f"✗ Base64 decoding failed: {e}")
            raise

    def test_stovelink_decoder_integration(self):
        """Test integration with StoveLinkDecoder"""
        print("\n=== Test 7: StoveLink Decoder Integration ===")
        self.test_count += 1

        decoder = StoveLinkDecoder()

        # Create a test packet
        header = struct.pack(
            "<IIfHH",
            0,  # sequence_id
            1000,  # timestamp_ms
            50.0,  # vent_pct
            300,  # combustion_time
            0,
        )  # reserved

        # Create thermal data with left-right gradient
        thermal_data = []
        for i in range(768):
            # Create pattern: left side cooler, right side hotter
            pixel_idx = i % 32  # Column index (0-31)
            temp = 20.0 + (pixel_idx / 32.0) * 60.0  # 20-80°C gradient
            thermal_data.append(int(temp * 10))  # Convert to 0.1°C units

        body = struct.pack("<768H", *thermal_data)
        packet = header + body

        # Decode packet
        decoded = decoder.decode_packet(packet)
        thermal_frame = decoded["thermal_frame"]

        print(f"Decoded thermal frame shape: {thermal_frame.shape}")
        print(f"Frame min temp: {thermal_frame.min():.1f}°C, max temp: {thermal_frame.max():.1f}°C")

        # Generate RGB image (should be flipped)
        rgb_data = self.generator.frame_to_rgb(thermal_frame)

        print(f"Generated RGB shape: {rgb_data.shape}")

        # After flip, left should be hot, right should be cool
        left_pixels = rgb_data[:, :8, :]
        right_pixels = rgb_data[:, -8:, :]

        left_avg_r = np.mean(left_pixels[:, :, 0])
        right_avg_r = np.mean(right_pixels[:, :, 0])

        print(f"After flip - Left R avg: {left_avg_r:.1f}, Right R avg: {right_avg_r:.1f}")
        assert left_avg_r > right_avg_r, "Flip should reverse temperature gradient"

        print("✓ StoveLink decoder integration test passed")
        self.passed_count += 1

    def run_all_tests(self):
        """Run all test cases"""
        print("=" * 60)
        print("ThermalImageGenerator Test Suite")
        print("=" * 60)

        try:
            self.test_frame_to_rgb_shape()
            self.test_horizontal_flip()
            self.test_grayscale_colormap()
            self.test_ironbow_colormap()
            self.test_encode_bmp()
            self.test_base64_encoding()
            self.test_stovelink_decoder_integration()

            print("\n" + "=" * 60)
            print(f"✓ All tests passed! ({self.passed_count}/{self.test_count})")
            print("=" * 60)
            return True
        except AssertionError as e:
            print(f"\n✗ Test failed: {e}")
            print(f"Tests passed: {self.passed_count}/{self.test_count}")
            return False
        except Exception as e:
            print(f"\n✗ Unexpected error: {e}")
            import traceback

            traceback.print_exc()
            return False


if __name__ == "__main__":
    tester = TestThermalImageGenerator()
    success = tester.run_all_tests()
    sys.exit(0 if success else 1)
