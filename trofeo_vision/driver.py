"""LY Bulk USB driver for Thermalright Trofeo Vision 9.16" (VID 0x0416 / PID 0x5408).

Reference: Lexonight1/thermalright-trcc-linux doc/PROTOCOL_USBLCDNEW.md
            Abigor-DIY/Open-Trofeo-LCD trofeo_lcd.py (single-file reference)

Frame format (JPEG, 462x1920 portrait):
    - 16-byte header per 512-byte USB chunk
    - Chunks sent in 4096-byte bursts (8 chunks at a time)
    - Total chunks padded to multiple of 4
    - Final ACK: read 512 bytes from EP 0x81

This module is the *only* file that talks USB. Everything else can run with
a saved JPEG on disk via Driver.dry_run.

Sleep/wake resilience: ``open()`` retries once on ``set_configuration``
ACCESS errors after disposing the candidate device's resources. This
covers the common post-suspend case where Windows has a stale claim on
the interface until the bus reset completes (typically <500 ms after
wake). Without this, every retry gets ``[Errno 13] Access denied`` until
the user manually quits and relaunches.
"""

from __future__ import annotations

import io
import logging
import struct
import time as _time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from PIL import Image

log = logging.getLogger(__name__)


# USB identifiers
TROFEO_VID = 0x0416
TROFEO_PID = 0x5408

# libusb errno for "Access denied" — happens on set_configuration when the
# interface is still claimed by another process (TRCC) or the kernel driver
# hasn't released yet (post-suspend).
LIBUSB_ERROR_ACCESS = 13


class DriverError(RuntimeError):
    """Raised when the USB device can't be opened or a frame fails."""


@dataclass
class FrameStats:
    chunks: int = 0
    bytes: int = 0
    duration_ms: float = 0.0


class Driver:
    """High-level push of one JPEG frame to the Trofeo Vision.

    Lifecycle:
        driver = Driver()           # dry-run by default
        driver.open()               # tries to claim USB
        driver.push_frame(jpeg)
        driver.close()
    """

    CHUNK_PAYLOAD = 496            # 512 - 16 header
    BURST_CHUNKS = 8               # 8 * 512 = 4096 bytes per USB write
    EP_OUT = 0x09
    EP_IN = 0x81

    def __init__(self, vendor_id: int = TROFEO_VID, product_id: int = TROFEO_PID,
                 jpeg_quality: int = 85, timeout_ms: int = 5000):
        self.vendor_id = vendor_id
        self.product_id = product_id
        self.jpeg_quality = jpeg_quality
        self.timeout_ms = timeout_ms
        self._dev = None
        self._usb = None
        self._claimed = False

    # ── Lifecycle ────────────────────────────────────────────────────
    def open(self) -> None:
        """Claim the USB interface. Raises DriverError if it fails.

        On ``set_configuration`` ACCESS errors (typical after sleep/wake),
        disposes the candidate device's resources, detaches any kernel
        driver, waits briefly, and retries once. If still failing, raises
        so the tray reconnect loop can back off.
        """
        try:
            import usb.core          # noqa: F401 — lazy import keeps dry-run pure
            import usb.util
        except ImportError as e:
            raise DriverError(
                "pyusb not installed. Run: pip install pyusb libusb-package"
            ) from e

        import usb.core

        last_err: Optional[BaseException] = None
        for attempt in (1, 2):
            dev = usb.core.find(
                idVendor=self.vendor_id, idProduct=self.product_id,
            )
            if dev is None:
                raise DriverError(
                    f"Device {self.vendor_id:04X}:{self.product_id:04X} "
                    "not found. Is TRCC closed? Is the WinUSB driver "
                    "bound (try Zadig)?"
                )

            # Detach kernel driver if any (Windows usually doesn't have one
            # for WinUSB, but be safe).
            try:
                if dev.is_kernel_driver_active(0):  # type: ignore[attr-defined]
                    try:
                        dev.detach_kernel_driver(0)  # type: ignore[attr-defined]
                    except Exception as e:  # noqa: BLE001
                        log.debug("detach_kernel_driver: %s", e)
            except Exception:  # noqa: BLE001
                pass

            # Force-dispose any libusb-level resources on this dev before
            # set_configuration. After sleep/wake the USB bus reset leaves
            # libusb with stale endpoint handles; without this, retry
            # loops forever with [Errno 13] Access denied.
            try:
                usb.util.dispose_resources(dev)  # type: ignore[arg-type]
            except Exception:  # noqa: BLE001
                pass

            try:
                dev.set_configuration()  # type: ignore[attr-defined]
            except Exception as e:  # noqa: BLE001
                last_err = e
                errno = getattr(e, "errno", None)
                if errno == LIBUSB_ERROR_ACCESS and attempt == 1:
                    log.warning(
                        "set_configuration ACCESS denied (attempt 1/2) — "
                        "USB device still held, will retry after %.1fs",
                        0.5,
                    )
                    _time.sleep(0.5)
                    continue
                raise DriverError(
                    f"set_configuration failed: {e}"
                ) from e

            try:
                usb.util.claim_interface(dev, 0)  # type: ignore[arg-type]
                self._claimed = True
            except Exception as e:  # noqa: BLE001
                raise DriverError(
                    f"Could not claim USB interface — TRCC may still be "
                    f"running: {e}"
                ) from e

            self._dev = dev
            log.info("USB device %04X:%04X opened", self.vendor_id, self.product_id)

            # Init handshake: send 2048 bytes, read 512.
            try:
                self._handshake()
            except DriverError:
                # Handshake can fail on a half-recovered bus — release
                # cleanly so the caller can retry fresh.
                self.close()
                if attempt == 1:
                    log.warning(
                        "Handshake failed on attempt 1/2, retrying once"
                    )
                    _time.sleep(0.3)
                    continue
                raise
            return

        # Defensive: should not reach here because the inner branches raise
        # or return. But keep the explicit raise for type-checkers.
        raise DriverError(
            f"open() failed after retries: {last_err}"
        ) if last_err else DriverError("open() failed: unknown")

    def close(self) -> None:
        if self._dev is None:
            return
        if self._claimed:
            try:
                import usb.util
                usb.util.release_interface(self._dev, 0)  # type: ignore[arg-type]
            except Exception:  # noqa: BLE001
                pass
        # Force-dispose libusb-level resources so the next open() starts
        # from a clean slate — this is what fixes the persistent
        # "Access denied" loop after sleep/wake.
        try:
            import usb.util
            usb.util.dispose_resources(self._dev)  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001
            pass
        try:
            self._dev.attach_kernel_driver(0)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass
        self._dev = None
        self._claimed = False

    # ── Frame push ───────────────────────────────────────────────────
    def push_frame(self, jpeg_data: bytes) -> FrameStats:
        """Push one JPEG frame to the device. Returns timing stats."""
        if self._dev is None:
            raise DriverError("Driver not opened — call open() first")
        return self._send_jpeg_chunked(jpeg_data)

    def encode_jpeg(self, image) -> bytes:
        """Encode a PIL.Image to baseline JPEG at our preferred quality.

        The Trofeo Vision 9.16" has a physical 1920x462 landscape LCD.
        The device firmware expects a 462x1920 portrait JPEG and rotates
        the decoded buffer 90° clockwise for display. To compensate, we
        rotate our portrait render 90° counter-clockwise before encoding —
        after the device's CW rotation, the layout appears upright.

        Net effect: render in portrait (easier to design) → push landscape →
        firmware rotates → user sees portrait.
        """
        # ROTATE_90 = 90° counter-clockwise (PIL Image.ROTATE_90 constant).
        image = image.transpose(Image.ROTATE_90)
        buf = io.BytesIO()
        # subsampling=0 → 4:4:4, finest chroma. LCD is small so cost is negligible.
        image.save(buf, format="JPEG", quality=self.jpeg_quality, subsampling=0)
        return buf.getvalue()

    # ── Internals ────────────────────────────────────────────────────
    def _handshake(self):
        pkt = bytearray(2048)
        pkt[0] = 0x02
        pkt[1] = 0xFF
        pkt[8] = 0x01
        try:
            self._dev.write(self.EP_OUT, bytes(pkt), self.timeout_ms)  # type: ignore[attr-defined]
            ack = self._dev.read(self.EP_IN, 512, self.timeout_ms)  # type: ignore[attr-defined]
        except Exception as e:  # noqa: BLE001
            raise DriverError(f"Handshake failed: {e}") from e
        if not ack or ack[0] != 0x03 or ack[1] != 0xFF:
            raise DriverError(f"Bad handshake ACK: {bytes(ack[:16])!r}")

    def _send_jpeg_chunked(self, jpeg: bytes) -> FrameStats:
        import time
        start = time.perf_counter()

        total = len(jpeg)
        chunks_needed = (total + self.CHUNK_PAYLOAD - 1) // self.CHUNK_PAYLOAD
        # Pad chunk count to multiple of 4 per protocol spec.
        total_chunks = ((chunks_needed + 3) // 4) * 4

        chunks: list[bytes] = []
        offset = 0
        for i in range(total_chunks):
            payload = jpeg[offset:offset + self.CHUNK_PAYLOAD]
            payload = payload + b"\x00" * (self.CHUNK_PAYLOAD - len(payload))
            # 16-byte header. Layout:
            #   [0]       0x01                B
            #   [1]       0xFF                B
            #   [2..5]    total (LE u32)      I
            #   [6..7]    payload len (LE u16) H
            #   [8]       0x01 flag           B
            #   [9..10]   total_chunks (LE u16) H
            #   [11..12]  chunk_index (LE u16) H
            #   [13..15]  padding             3s
            header = struct.pack(
                "<BBIHBHH3s",
                0x01,                       # [0]
                0xFF,                       # [1]
                total,                      # [2..5]
                len(payload),               # [6..7]
                0x01,                       # [8]
                total_chunks,               # [9..10]
                i,                          # [11..12]
                b"\x00\x00\x00",            # [13..15]
            )
            chunks.append(header + payload)
            offset += self.CHUNK_PAYLOAD

        # Send in 4096-byte bursts.
        for burst_start in range(0, len(chunks), self.BURST_CHUNKS):
            burst = b"".join(chunks[burst_start:burst_start + self.BURST_CHUNKS])
            try:
                self._dev.write(self.EP_OUT, burst, self.timeout_ms)  # type: ignore[attr-defined]
            except Exception as e:  # noqa: BLE001
                raise DriverError(f"USB write failed at burst {burst_start}: {e}") from e

        # Read final ACK.
        try:
            ack = self._dev.read(self.EP_IN, 512, self.timeout_ms)  # type: ignore[attr-defined]
            log.debug("ACK: %s", bytes(ack[:16]).hex())
        except Exception as e:  # noqa: BLE001
            raise DriverError(f"Final ACK read failed: {e}") from e

        elapsed = (time.perf_counter() - start) * 1000.0
        return FrameStats(chunks=total_chunks, bytes=total, duration_ms=elapsed)

    # ── Convenience: context manager ─────────────────────────────────
    def __enter__(self):
        self.open()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False
