"""Cross-platform Bulk USB transport for Actions Semiconductor devices (VID 0x10D6).

Uses zero-dependency native Linux usbdevfs ioctls on Linux, with an automatic
pyusb fallback on macOS and Windows.
"""
from __future__ import annotations

import ctypes
import glob
import os
import sys
from dataclasses import dataclass
from typing import List, Optional

VID_ACTIONS = 0x10D6
PID_NORMAL = 0x1101
PID_ADFU = 0x10D6

EP_OUT = 0x01
EP_IN = 0x82

# Linux usbdevfs ioctl numbers (x86_64 / aarch64)
USBDEVFS_BULK = 0xC0185502
USBDEVFS_CLAIMINTERFACE = 0x8004550F
USBDEVFS_RELEASEINTERFACE = 0x80045510
USBDEVFS_IOCTL = 0xC0105512
USBDEVFS_DISCONNECT = 0x5516
USBDEVFS_CLEAR_HALT = 0x80045515


class _UsbdevfsBulktransfer(ctypes.Structure):
    _fields_ = [
        ("ep", ctypes.c_uint),
        ("len", ctypes.c_uint),
        ("timeout", ctypes.c_uint),
        ("data", ctypes.c_void_p),
    ]


class _UsbdevfsIoctl(ctypes.Structure):
    _fields_ = [
        ("ifno", ctypes.c_int),
        ("ioctl_code", ctypes.c_int),
        ("data", ctypes.c_void_p),
    ]


@dataclass
class ActionsDeviceInfo:
    vid: int
    pid: int
    bus: int
    address: int
    speed: str
    usb_path: str
    backend: str  # "linux" or "pyusb"
    _pyusb_dev: object = None

    @property
    def vid_pid(self) -> str:
        return f"{self.vid:04x}:{self.pid:04x}"


def _read_sysfs(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return ""


def find_actions_devices() -> List[ActionsDeviceInfo]:
    """Return all connected Actions (10d6:*) USB devices."""
    if sys.platform.startswith("linux"):
        found: List[ActionsDeviceInfo] = []
        for devdir in sorted(glob.glob("/sys/bus/usb/devices/*")):
            vid_s = _read_sysfs(os.path.join(devdir, "idVendor")).lower()
            if vid_s != "10d6":
                continue
            pid_s = _read_sysfs(os.path.join(devdir, "idProduct")).lower()
            bus_s = _read_sysfs(os.path.join(devdir, "busnum"))
            dev_s = _read_sysfs(os.path.join(devdir, "devnum"))
            if not bus_s or not dev_s or int(dev_s) <= 0:
                continue
            bus = int(bus_s)
            addr = int(dev_s)
            speed = _read_sysfs(os.path.join(devdir, "speed"))
            found.append(
                ActionsDeviceInfo(
                    vid=int(vid_s, 16),
                    pid=int(pid_s, 16),
                    bus=bus,
                    address=addr,
                    speed=speed,
                    usb_path=f"/dev/bus/usb/{bus:03d}/{addr:03d}",
                    backend="linux",
                )
            )
        return found

    # macOS / Windows fallback via pyusb
    try:
        import usb.core  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "pyusb is required on non-Linux platforms. Install it with: pip install pyusb"
        ) from exc

    found = []
    for dev in usb.core.find(find_all=True, idVendor=VID_ACTIONS):
        bus = getattr(dev, "bus", 0) or 0
        addr = getattr(dev, "address", 0) or 0
        found.append(
            ActionsDeviceInfo(
                vid=dev.idVendor,
                pid=dev.idProduct,
                bus=bus,
                address=addr,
                speed="usb",
                usb_path=f"pyusb://{bus:03d}/{addr:03d}",
                backend="pyusb",
                _pyusb_dev=dev,
            )
        )
    return found


class UsbTransport:
    """Opened bulk-USB handle for an Actions device."""

    def __init__(self, info: ActionsDeviceInfo):
        self.info = info
        self._fd: Optional[int] = None
        self._pyusb_dev = info._pyusb_dev

    def open(self) -> "UsbTransport":
        if self.info.backend == "linux":
            import fcntl

            try:
                fd = os.open(self.info.usb_path, os.O_RDWR)
            except PermissionError as exc:
                raise PermissionError(
                    f"Permission denied opening {self.info.usb_path}. "
                    "Install udev/99-kagami-usb.rules or run with appropriate USB permissions."
                ) from exc
            try:
                io = _UsbdevfsIoctl(ifno=0, ioctl_code=USBDEVFS_DISCONNECT, data=0)
                fcntl.ioctl(fd, USBDEVFS_IOCTL, io)
            except OSError:
                pass
            fcntl.ioctl(fd, USBDEVFS_CLAIMINTERFACE, ctypes.c_uint(0))
            self._fd = fd
            return self

        import usb.util  # type: ignore

        dev = self._pyusb_dev
        try:
            if dev.is_kernel_driver_active(0):
                dev.detach_kernel_driver(0)
        except (NotImplementedError, Exception):
            pass
        try:
            dev.set_configuration()
        except Exception:
            pass
        usb.util.claim_interface(dev, 0)
        return self

    def close(self) -> None:
        if self.info.backend == "linux":
            if self._fd is not None:
                import fcntl

                try:
                    fcntl.ioctl(self._fd, USBDEVFS_RELEASEINTERFACE, ctypes.c_uint(0))
                except OSError:
                    pass
                try:
                    os.close(self._fd)
                except OSError:
                    pass
                self._fd = None
            return

        if self._pyusb_dev is not None:
            import usb.util  # type: ignore

            try:
                usb.util.release_interface(self._pyusb_dev, 0)
            except Exception:
                pass

    def __enter__(self) -> "UsbTransport":
        return self.open()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def bulk_out(self, data: bytes, timeout_ms: int = 3000) -> int:
        if self.info.backend == "linux":
            import fcntl

            assert self._fd is not None
            buf = (ctypes.c_ubyte * len(data))(*data)
            xfer = _UsbdevfsBulktransfer(
                ep=EP_OUT, len=len(data), timeout=timeout_ms, data=ctypes.addressof(buf)
            )
            return int(fcntl.ioctl(self._fd, USBDEVFS_BULK, xfer))

        return int(self._pyusb_dev.write(EP_OUT, data, timeout=timeout_ms))

    def bulk_in(self, length: int = 512, timeout_ms: int = 3000) -> bytes:
        if self.info.backend == "linux":
            import fcntl

            assert self._fd is not None
            buf = (ctypes.c_ubyte * length)()
            xfer = _UsbdevfsBulktransfer(
                ep=EP_IN, len=length, timeout=timeout_ms, data=ctypes.addressof(buf)
            )
            ret = int(fcntl.ioctl(self._fd, USBDEVFS_BULK, xfer))
            return bytes(buf[:ret])

        return bytes(self._pyusb_dev.read(EP_IN, length, timeout=timeout_ms))
