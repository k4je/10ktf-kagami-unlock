"""USB client for the live Kagami MIPS32 SRAM NAND probe/writer payload."""
from __future__ import annotations

import struct
from typing import List, Tuple

from .usb_transport import ActionsDeviceInfo, UsbTransport

CMD_MAGIC = 0x444D434B
RSP_MAGIC = 0x5053524B
UNLOCK_MAGIC = 0xA55A1936

OP_PING = 1
OP_READ_RAW = 2
OP_READ_SECTOR = 3
OP_READ_ID = 4
OP_LOAD_CHUNK = 7
OP_ERASE_BLOCK = 8
OP_PROG_PAGE_START = 9
OP_PROG_SECTOR = 10
OP_PROG_PAGE_FINISH = 11
OP_WDT_REBOOT = 15

EXPECTED_JEDEC_PREFIX = bytes.fromhex("2c44444ba9")  # SpecTek/Micron PFE13 (MT29F32G08CBADA)


class NandClient:
    """Communicates with the KAGNAND1 SRAM payload over Bulk USB."""

    def __init__(self, dev: ActionsDeviceInfo):
        self._usb = UsbTransport(dev)
        self._seq = 1

    def __enter__(self) -> "NandClient":
        self._usb.open()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._usb.close()

    def cmd(
        self,
        op: int,
        arg0: int = 0,
        arg1: int = 0,
        arg2: int = 0,
        arg3: int = 0,
        arg4: int = 0,
        payload: bytes = b"",
        timeout_ms: int = 5000,
    ) -> dict:
        seq = self._seq
        self._seq += 1
        pkt = struct.pack("<8I", CMD_MAGIC, op, arg0, arg1, arg2, arg3, arg4, seq) + payload
        n = self._usb.bulk_out(pkt, timeout_ms=timeout_ms)
        if n != len(pkt):
            raise RuntimeError(f"Short bulk-OUT write: {n} != {len(pkt)}")

        buf = bytearray(self._usb.bulk_in(512, timeout_ms=timeout_ms))
        if len(buf) < 32:
            raise RuntimeError(f"Short bulk-IN header ({len(buf)}B): {buf.hex()}")

        magic, rseq, rc, dlen, r4, r5, r6, r7 = struct.unpack("<8I", buf[:32])
        if magic != RSP_MAGIC or rseq != seq:
            raise RuntimeError(f"Bad response magic=0x{magic:08x} seq={rseq} (expected {seq})")

        total = 32 + dlen
        while len(buf) < total:
            chunk = self._usb.bulk_in(512, timeout_ms=timeout_ms)
            if not chunk:
                break
            buf.extend(chunk)

        return {
            "rc": rc if rc < 0x80000000 else rc - 0x100000000,
            "dlen": dlen,
            "r4": r4,
            "r5": r5,
            "r6": r6,
            "r7": r7,
            "data": bytes(buf[32 : 32 + dlen]),
        }

    def ping(self) -> dict:
        r = self.cmd(OP_PING)
        if r["rc"] != 0:
            raise RuntimeError(f"SRAM payload ping failed: rc={r['rc']}")
        return r

    def read_id(self, addr: int = 0x00, ce: int = 0) -> bytes:
        r = self.cmd(OP_READ_ID, addr, ce)
        if r["rc"] != 0:
            raise RuntimeError(f"OP_READ_ID(0x{addr:02x}) failed: rc={r['rc']}")
        return r["data"]

    def verify_nand_chip(self) -> Tuple[bytes, bytes]:
        jedec = self.read_id(0x00)
        onfi = self.read_id(0x20)
        if not jedec.startswith(EXPECTED_JEDEC_PREFIX) or not onfi.startswith(b"ONFI"):
            raise RuntimeError(
                f"Unsupported NAND flash! JEDEC={jedec.hex()} ONFI={onfi!r} "
                f"(expected SpecTek/Micron PFE13 {EXPECTED_JEDEC_PREFIX.hex()} + ONFI)"
            )
        return jedec, onfi

    def read_raw_page(self, row: int, total_bytes: int, ce: int = 0) -> bytes:
        out = bytearray()
        for col in range(0, total_bytes, 1024):
            chunk_len = min(1024, total_bytes - col)
            flags = (ce << 8) | (0x40 if col > 0 else 0)
            r = self.cmd(OP_READ_RAW, row, col, 0, chunk_len, flags)
            if r["rc"] != 0:
                raise RuntimeError(f"read_raw row={row} col={col} rc={r['rc']}")
            out.extend(r["data"])
        return bytes(out)

    def read_sector(
        self,
        row: int,
        col_data: int,
        col_oob: int,
        length: int,
        bch: int = 2,
        udat: int = 3,
        ecc: int = 1,
        scram: int = 1,
        skip_0030: int = 0,
        ce: int = 0,
    ) -> dict:
        flags = (
            (bch & 3)
            | ((udat & 3) << 2)
            | ((ecc & 1) << 4)
            | ((scram & 1) << 5)
            | ((skip_0030 & 1) << 6)
            | ((ce & 3) << 8)
        )
        return self.cmd(OP_READ_SECTOR, row, col_data, col_oob, length, flags)

    def read_page_verified(
        self, row: int, bch: int = 2, udat: int = 3, ecc: int = 1, scram: int = 1, oob_stride: int = 78
    ) -> Tuple[bytes, List[int], List[int], List[int]]:
        data = bytearray(8192)
        ecc_list: List[int] = []
        u0_list: List[int] = []
        u1_list: List[int] = []
        for s in range(8):
            r = self.read_sector(
                row,
                s * 1024,
                8192 + s * oob_stride,
                1024,
                bch=bch,
                udat=udat,
                ecc=ecc,
                scram=scram,
                skip_0030=(1 if s > 0 else 0),
            )
            if r["rc"] != 0:
                raise RuntimeError(f"read_page_verified row={row} s={s} rc={r['rc']}")
            data[s * 1024 : (s + 1) * 1024] = r["data"]
            ecc_list.append(r["r4"] & 0x3F)
            u0_list.append(r["r6"])
            u1_list.append(r["r7"])
        return bytes(data), ecc_list, u0_list, u1_list

    def _load_sector_buf(self, sector_1024: bytes) -> None:
        assert len(sector_1024) == 1024
        last_r4 = 0
        for off in range(0, 1024, 256):
            r = self.cmd(OP_LOAD_CHUNK, off, 256, 0, 0, 0, payload=sector_1024[off : off + 256])
            if r["rc"] != 0:
                raise RuntimeError(f"OP_LOAD_CHUNK off={off} failed: rc={r['rc']}")
            last_r4 = r["r4"]
        expected_sum = sum(struct.unpack("<256I", sector_1024)) & 0xFFFFFFFF
        if last_r4 != expected_sum:
            raise RuntimeError(
                f"SRAM sector checksum mismatch: 0x{last_r4:08x} != 0x{expected_sum:08x}"
            )

    def erase_block(self, blk: int) -> int:
        if blk < 48:
            raise ValueError(f"Refusing to erase protected bootloader/FTL block {blk} (< 48)")
        r = self.cmd(OP_ERASE_BLOCK, blk, UNLOCK_MAGIC)
        if r["rc"] != 0:
            raise RuntimeError(f"OP_ERASE_BLOCK blk={blk} failed: rc={r['rc']} status=0x{r['r4']:08x}")
        return int(r["r4"])

    def program_page(
        self,
        row: int,
        page_8192: bytes,
        u0: int,
        u1: int,
        bch: int = 2,
        udat: int = 3,
        ecc: int = 1,
        scram: int = 1,
        oob_stride: int = 78,
    ) -> int:
        blk = row // 256
        if blk < 48:
            raise ValueError(f"Refusing to program protected block {blk} (< 48)")
        assert len(page_8192) == 8192
        r0 = self.cmd(OP_PROG_PAGE_START, row, UNLOCK_MAGIC)
        if r0["rc"] != 0:
            raise RuntimeError(f"OP_PROG_PAGE_START row={row} failed: rc={r0['rc']}")
        flags = (bch & 3) | ((udat & 3) << 2) | ((ecc & 1) << 4) | ((scram & 1) << 5)
        for s in range(8):
            self._load_sector_buf(page_8192[s * 1024 : (s + 1) * 1024])
            col_pair = ((s * 1024) & 0xFFFF) | (((8192 + s * oob_stride) & 0xFFFF) << 16)
            su0 = (u0 & 0xFFFFFFFF) if s == 0 else 0xFFFFFFFF
            su1 = (u1 & 0xFFFFFFFF) if s == 0 else 0xFFFFFFFF
            rs = self.cmd(OP_PROG_SECTOR, col_pair, su0, su1, flags, UNLOCK_MAGIC)
            if rs["rc"] != 0:
                raise RuntimeError(f"OP_PROG_SECTOR row={row} s={s} failed: rc={rs['rc']}")
        rf = self.cmd(OP_PROG_PAGE_FINISH, UNLOCK_MAGIC)
        if rf["rc"] != 0:
            raise RuntimeError(
                f"OP_PROG_PAGE_FINISH row={row} failed: rc={rf['rc']} status=0x{rf['r4']:08x}"
            )
        return int(rf["r4"])

    def wdt_reboot(self) -> None:
        try:
            self.cmd(OP_WDT_REBOOT, timeout_ms=1000)
        except Exception:
            pass
