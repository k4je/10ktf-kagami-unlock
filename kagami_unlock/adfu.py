"""Actions Semiconductor ATJ229R2 (0x5009) ADFU Boot ROM protocol."""
from __future__ import annotations

import pathlib
import struct
import time
from typing import Optional

from .usb_transport import (
    PID_ADFU,
    PID_NORMAL,
    ActionsDeviceInfo,
    UsbTransport,
    find_actions_devices,
)

LOAD_ADDRESS = 0xB4061000
MAX_PACKET = 512

OPCODE_UPLOAD = 0x05
OPCODE_EXECUTE = 0x10

SUCCESS_CSW = b"USBS" + bytes(9)
GETINFO_CBW = bytes.fromhex(
    "555342430000000012000000800010cc000000000000120000000000000000"
)
EXPECTED_ATJ229R2_ID = bytes.fromhex("004341444655445009460000000000000000")

PAYLOAD_PATH = pathlib.Path(__file__).resolve().parent / "payload" / "nand_probe.encoded.bin"


class AdfuError(RuntimeError):
    """Raised on any ADFU Boot ROM protocol mismatch or refusal."""


def make_cbw(tag: int, xfer_len: int, flags: int, lun: int, cdb: bytes) -> bytes:
    return (
        struct.pack("<4sIIBBB", b"USBC", tag, xfer_len, flags, lun, len(cdb))
        + cdb
        + bytes(16 - len(cdb))
    )


def build_adfu_cbw(opcode: int, address: int, length: int) -> bytes:
    cdb = bytes([opcode]) + struct.pack("<III", address, length, 0) + bytes(3)
    return b"USBC" + struct.pack("<II", 0, length) + bytes([0, 0, 16]) + cdb


def _parse_strict_csw(data: bytes) -> None:
    if len(data) != 13 or data[:4] != b"USBS":
        raise AdfuError(f"Invalid CSW ({len(data)}B): {data.hex()}")
    tag, residue, status = struct.unpack("<IIB", data[4:])
    if tag != 0 or residue != 0 or status != 0:
        raise AdfuError(f"ADFU rejected command: tag={tag} residue={residue} status={status}")


def _read_exact(usb: UsbTransport, size: int, timeout_ms: int = 5000) -> bytes:
    buf = bytearray()
    for _ in range(16):
        chunk = usb.bulk_in(MAX_PACKET, timeout_ms=timeout_ms)
        if not chunk:
            raise AdfuError("Empty bulk-IN transfer")
        buf.extend(chunk)
        if len(buf) == size:
            return bytes(buf)
        if len(buf) > size:
            raise AdfuError(f"Unexpected trailing bytes: got {len(buf)} > {size}")
    raise AdfuError(f"Incomplete bulk-IN transfer: got {len(buf)}/{size}")


def switch_normal_to_adfu(
    dev: ActionsDeviceInfo, wait_timeout_s: float = 10.0
) -> ActionsDeviceInfo:
    """Reboot a normal-mode Kagami (10d6:1101) into silicon Boot ROM ADFU (10d6:10d6)."""
    with UsbTransport(dev) as usb:
        for tag, xlen, flags, cdb in [
            (1, 18, 0x80, bytes([0xCC, 0, 0, 0, 0, 0, 0, 18] + [0] * 8)),
            (2, 2, 0x80, bytes([0xCB, 0x21, 0, 0, 0, 0, 0, 2] + [0] * 8)),
            (3, 0, 0x00, bytes([0x1E, 0, 0, 0, 0, 0])),
            (4, 0, 0x00, bytes([0x1B, 0, 0, 0, 0x02, 0])),
        ]:
            usb.bulk_out(make_cbw(tag, xlen, flags, 0, cdb), timeout_ms=2000)
            if xlen > 0:
                _ = usb.bulk_in(MAX_PACKET, timeout_ms=2000)
            _ = usb.bulk_in(MAX_PACKET, timeout_ms=2000)

    deadline = time.monotonic() + wait_timeout_s
    while time.monotonic() < deadline:
        for cand in find_actions_devices():
            if cand.pid == PID_ADFU:
                return cand
        time.sleep(0.2)
    raise AdfuError("Timed out waiting for Kagami to re-enumerate in ADFU mode (10d6:10d6)")


def wait_for_adfu(poll_timeout_s: float = 60.0, verbose: bool = True) -> ActionsDeviceInfo:
    """Wait for a Kagami in either 10d6:1101 (switches to ADFU) or 10d6:10d6."""
    deadline = time.monotonic() + poll_timeout_s
    logged_wait = False
    while time.monotonic() < deadline:
        for dev in find_actions_devices():
            if dev.pid == PID_ADFU:
                return dev
            if dev.pid == PID_NORMAL:
                if verbose:
                    print(
                        f"Found Kagami in normal mode ({dev.vid_pid} at {dev.usb_path}) — switching to ADFU...",
                        flush=True,
                    )
                try:
                    return switch_normal_to_adfu(dev)
                except OSError:
                    time.sleep(0.25)
        if verbose and not logged_wait:
            print("Waiting for Kagami USB device (10d6:1101 or 10d6:10d6)...", flush=True)
            logged_wait = True
        time.sleep(0.2)
    raise AdfuError("No Kagami USB device detected before timeout.")


def upload_sram_payload(
    dev: ActionsDeviceInfo,
    payload_path: Optional[pathlib.Path] = None,
    timeout_ms: int = 5000,
) -> bytes:
    """Verify ATJ229R2 Boot ROM identity, upload SRAM payload, execute it, and return KAGNAND1 greeting."""
    path = payload_path or PAYLOAD_PATH
    encoded = path.read_bytes()
    if len(encoded) % 512 != 0 or not (512 <= len(encoded) <= 65536):
        raise AdfuError(f"Invalid SRAM payload size: {len(encoded)}")

    with UsbTransport(dev) as usb:
        # 1. Verify Boot ROM 0xCC identity strictly
        usb.bulk_out(GETINFO_CBW, timeout_ms=timeout_ms)
        ident = _read_exact(usb, len(EXPECTED_ATJ229R2_ID), timeout_ms=timeout_ms)
        _parse_strict_csw(_read_exact(usb, 13, timeout_ms=timeout_ms))
        if ident != EXPECTED_ATJ229R2_ID:
            raise AdfuError(
                f"Unsupported Actions Boot ROM ID: {ident.hex()} (expected ATJ229R2 0x5009 rev F)"
            )

        # 2. Upload SRAM payload (0x05)
        usb.bulk_out(build_adfu_cbw(OPCODE_UPLOAD, LOAD_ADDRESS, len(encoded)), timeout_ms=timeout_ms)
        for off in range(0, len(encoded), MAX_PACKET):
            chunk = encoded[off : off + MAX_PACKET]
            written = usb.bulk_out(chunk, timeout_ms=timeout_ms)
            if written != len(chunk):
                raise AdfuError(f"Short bulk-OUT write: {written}/{len(chunk)}")
        _parse_strict_csw(_read_exact(usb, 13, timeout_ms=timeout_ms))

        # 3. Execute SRAM payload (0x10) and read 2048B KAGNAND1 greeting
        usb.bulk_out(build_adfu_cbw(OPCODE_EXECUTE, LOAD_ADDRESS, 0), timeout_ms=timeout_ms)
        header = b"KAGNAND1"
        csw_prefix = SUCCESS_CSW + header
        buf = bytearray()
        for _ in range(32):
            buf.extend(usb.bulk_in(MAX_PACKET, timeout_ms=timeout_ms))
            plain = len(buf) <= 2048 and header.startswith(buf[: min(len(buf), len(header))])
            prefixed = len(buf) <= 2048 + 13 and csw_prefix.startswith(
                buf[: min(len(buf), len(csw_prefix))]
            )
            if plain and len(buf) == 2048:
                return bytes(buf)
            if prefixed and len(buf) == 2048 + 13:
                _parse_strict_csw(bytes(buf[:13]))
                return bytes(buf[13:])
            if not plain and not prefixed:
                raise AdfuError(f"Unexpected execute reply: {buf[:32].hex()}")
        raise AdfuError("Incomplete KAGNAND1 greeting after SRAM execute")
