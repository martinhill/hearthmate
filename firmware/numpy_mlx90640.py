# SPDX-FileCopyrightText: 2019 ladyada for Adafruit Industries
#
# SPDX-License-Identifier: MIT

"""
MLX90640 driver with the temperature calculation vectorized with ulab.

A subclass of the forked adafruit_mlx90640 driver: the EEPROM calibration extraction and the
frame reads are the same, but _CalculateTo() works on all 768 pixels at once in ulab instead of a
Python loop per pixel. The per-pixel calibration, which doesn't change between frames, becomes
float arrays once at init. getFrame() fills a ulab float array of 768 instead of a list, and
reads the frame RAM into a ulab array (np.frombuffer) instead of unpacking it word by word.

mlx90640_bench.py compares the two drivers on the device (the bench_camera command), and
scripts/bench_mlx90640.py checks that they agree on the desktop.
"""

import math
import time

from ulab import numpy as np
import adafruit_logging as logging

import adafruit_mlx90640
from adafruit_mlx90640 import I2C_READ_LEN, OPENAIR_TA_SHIFT, SCALEALPHA

try:
    from typing import List, Optional, Union

    from busio import I2C
except ImportError:
    pass

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def _mask(flags):
    """Boolean ulab array from a list of bools"""
    return np.array([1.0 if flag else 0.0 for flag in flags], dtype=np.float) > 0.5


class MLX90640(adafruit_mlx90640.MLX90640):
    """MLX90640 with the temperature calculation in ulab: getFrame() takes a float array of 768"""

    def __init__(self, i2c_bus: I2C, address: int = 0x33) -> None:
        # Reused by every frame read (the base driver allocates 4 KB per read)
        self._addrbuf = bytearray(2)
        self._inbuf = bytearray(2 * I2C_READ_LEN)
        self._frame = np.zeros(834, dtype=np.uint16)
        super().__init__(i2c_bus, address)
        self._PreparePixelArrays()

    def _PreparePixelArrays(self) -> None:
        """Turn the per-pixel calibration extracted by the base driver into float arrays, and the
        reading patterns into masks of the pixels each subpage updates."""
        ktaScale = math.pow(2, self.ktaScale)
        kvScale = math.pow(2, self.kvScale)
        alphaScale = math.pow(2, self.alphaScale)
        self._offsetPx = np.array(self.offset, dtype=np.float)
        self._ktaPx = np.array([kta / ktaScale for kta in self.kta], dtype=np.float)
        self._kvPx = np.array([kv / kvScale for kv in self.kv], dtype=np.float)
        # alpha is 0 only for a broken pixel, which isn't calculated
        self._alphaPx = np.array(
            [SCALEALPHA * alphaScale / alpha if alpha else 0.0 for alpha in self.alpha],
            dtype=np.float,
        )

        ilPattern = [p // 32 - (p // 64) * 2 for p in range(768)]
        chessPattern = [ilPattern[p] ^ (p % 2) for p in range(768)]
        conversionPattern = [
            ((p + 2) // 4 - (p + 3) // 4 + (p + 1) // 4 - p // 4) * (1 - 2 * ilPattern[p])
            for p in range(768)
        ]
        # Correction for reading in the mode the device wasn't calibrated in
        self._ilChessPx = np.array(
            [
                self.ilChessC[2] * (2 * ilPattern[p] - 1)
                - self.ilChessC[1] * conversionPattern[p]
                for p in range(768)
            ],
            dtype=np.float,
        )

        # [interleaved, chess][subpage]: the pixels to calculate, and the pixels to write, which
        # also include the deviating pixels (set to -273.15 like the per-pixel loop does)
        bad = self.brokenPixels + self.outlierPixels
        self._badMask = _mask([p in bad for p in range(768)])
        self._calcMasks = []
        self._updateMasks = []
        for pattern in (ilPattern, chessPattern):
            self._calcMasks.append(
                [_mask([pattern[p] == sp and p not in bad for p in range(768)]) for sp in (0, 1)]
            )
            self._updateMasks.append(
                [_mask([pattern[p] == sp or p in bad for p in range(768)]) for sp in (0, 1)]
            )

    def getFrame(self, framebuf: np.ndarray) -> None:
        """Request both 'halves' of a frame from the sensor, merge them and calculate the
        temperature in C for each of 32x24 pixels, into the float array of 768 passed in."""
        emissivity = 0.95
        frame = self._frame

        # initially accept either subpage
        subpage = None

        for _ in range(2):
            t0 = time.monotonic()
            status = self._GetFrameData(frame, subpage)
            t1 = time.monotonic()
            if status < 0:
                raise RuntimeError("Frame data error")
            # For a MLX90640 in the open air the shift is -8 degC.
            t2 = time.monotonic()
            tr = self._GetTa(frame) - OPENAIR_TA_SHIFT
            self._CalculateTo(frame, emissivity, tr, framebuf)
            tend = time.monotonic()
            log = logger.warning if tend - t0 > 2.0 else logger.debug
            log(
                "_GetFrameData status, duration = %d %f _CalculateTo duration = %f",
                status,
                t1 - t0,
                tend - t2,
            )
            subpage = 0 if status else 1

    @staticmethod
    def _CheckDomain(values: np.ndarray, mask: np.ndarray) -> None:
        # The per-pixel loop's math.sqrt() raises ValueError on a bad frame (the camera retries
        # it), where np.sqrt() would return nan
        if np.min(np.where(mask, values, 0.0)) < 0:
            raise ValueError("math domain error")

    def _CalculateTo(
        self, frameData: np.ndarray, emissivity: float, tr: float, result: np.ndarray
    ) -> None:  # noqa: PLR0914
        # int(): numpy, which stands in for ulab on the desktop, indexes to uint16 scalars
        subPage = int(frameData[833])
        alphaCorrR = [0] * 4
        irDataCP = [0, 0]

        vdd = self._GetVdd(frameData)
        ta = self._GetTa(frameData)

        ta4 = ta + 273.15
        ta4 = ta4 * ta4
        ta4 = ta4 * ta4
        tr4 = tr + 273.15
        tr4 = tr4 * tr4
        tr4 = tr4 * tr4
        taTr = tr4 - (tr4 - ta4) / emissivity

        alphaCorrR[0] = 1 / (1 + self.ksTo[0] * 40)
        alphaCorrR[1] = 1
        alphaCorrR[2] = 1 + self.ksTo[1] * self.ct[2]
        alphaCorrR[3] = alphaCorrR[2] * (1 + self.ksTo[2] * (self.ct[3] - self.ct[2]))

        # --------- Gain calculation -----------------------------------
        gain = int(frameData[778])
        if gain > 32767:
            gain -= 65536
        gain = self.gainEE / gain

        # --------- To calculation -------------------------------------
        mode = (int(frameData[832]) & 0x1000) >> 5

        irDataCP[0] = int(frameData[776])
        irDataCP[1] = int(frameData[808])
        for i in range(2):
            if irDataCP[i] > 32767:
                irDataCP[i] -= 65536
            irDataCP[i] *= gain

        irDataCP[0] -= (
            self.cpOffset[0] * (1 + self.cpKta * (ta - 25)) * (1 + self.cpKv * (vdd - 3.3))
        )
        if mode == self.calibrationModeEE:
            irDataCP[1] -= (
                self.cpOffset[1] * (1 + self.cpKta * (ta - 25)) * (1 + self.cpKv * (vdd - 3.3))
            )
        else:
            irDataCP[1] -= (
                (self.cpOffset[1] + self.ilChessC[0])
                * (1 + self.cpKta * (ta - 25))
                * (1 + self.cpKv * (vdd - 3.3))
            )

        # All 768 pixels at once. The other subpage's pixels still hold their previous reading;
        # they are calculated too, and left out by the masks.
        calc = self._calcMasks[0 if mode == 0 else 1][subPage]
        update = self._updateMasks[0 if mode == 0 else 1][subPage]

        irData = np.array(frameData[:768], dtype=np.float)
        irData = np.where(irData > 32767, irData - 65536, irData)
        irData = irData * gain - self._offsetPx * (1 + self._ktaPx * (ta - 25)) * (
            1 + self._kvPx * (vdd - 3.3)
        )
        if mode != self.calibrationModeEE:
            irData = irData + self._ilChessPx
        irData = (irData - self.tgc * irDataCP[subPage]) / emissivity

        alphaCompensated = self._alphaPx * (1 + self.KsTa * (ta - 25))

        Sx = alphaCompensated * alphaCompensated * alphaCompensated * (
            irData + alphaCompensated * taTr
        )
        self._CheckDomain(Sx, calc)
        Sx = np.sqrt(np.sqrt(Sx)) * self.ksTo[1]

        To4 = irData / (alphaCompensated * (1 - self.ksTo[1] * 273.15) + Sx) + taTr
        self._CheckDomain(To4, calc)
        To = np.sqrt(np.sqrt(To4)) - 273.15

        # The extended temperature ranges: the correction of the range each pixel is in
        belowCt1 = To < self.ct[1]
        belowCt2 = To < self.ct[2]
        belowCt3 = To < self.ct[3]
        alphaCorr = np.where(
            belowCt1,
            alphaCorrR[0],
            np.where(belowCt2, alphaCorrR[1], np.where(belowCt3, alphaCorrR[2], alphaCorrR[3])),
        )
        ksTo = np.where(
            belowCt1,
            self.ksTo[0],
            np.where(belowCt2, self.ksTo[1], np.where(belowCt3, self.ksTo[2], self.ksTo[3])),
        )
        ct = np.where(
            belowCt1,
            self.ct[0],
            np.where(belowCt2, self.ct[1], np.where(belowCt3, self.ct[2], self.ct[3])),
        )

        To4 = irData / (alphaCompensated * alphaCorr * (1 + ksTo * (To - ct))) + taTr
        self._CheckDomain(To4, calc)
        To = np.sqrt(np.sqrt(To4)) - 273.15

        To = np.where(self._badMask, -273.15, To)
        result[:] = np.where(update, To, result)

    def _I2CReadWords(
        self, addr: int, buffer: Union[List[int], np.ndarray], *, end: Optional[int] = None
    ) -> None:
        if isinstance(buffer, list):
            # EEPROM and registers
            super()._I2CReadWords(addr, buffer, end=end)
            return
        if end is None:
            remainingWords = len(buffer)
        else:
            remainingWords = end
        offset = 0
        addrbuf = self._addrbuf
        inbuf = self._inbuf

        with self.i2c_device as i2c:
            while remainingWords:
                addrbuf[0] = addr >> 8  # MSB
                addrbuf[1] = addr & 0xFF  # LSB
                read_words = min(remainingWords, I2C_READ_LEN)
                i2c.write_then_readinto(addrbuf, inbuf, in_end=read_words * 2)  # in bytes
                outwords = np.frombuffer(inbuf, dtype=np.uint16, count=read_words)
                outwords.byteswap(inplace=True)
                buffer[offset : offset + read_words] = outwords
                offset += read_words
                remainingWords -= read_words
                addr += read_words
