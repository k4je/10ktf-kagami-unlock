"""Dynamic Actions FTL scanner for 10KTF Kagami 4 GiB parallel NAND.

Never hardcodes physical block numbers: scans the live NAND read-only to locate
LBN 0x07 (/mnt/diskc, CONFIG.BIN), LBN 0x08 (/mnt/diska, desktop.apm), and a
confirmed-erased unused block for the mandatory pre-write self-test.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .nand_client import NandClient
from .patches import DESKTOP_ANCHOR_OFFSET, DESKTOP_ANCHOR_BYTES, parse_config_entries

TOTAL_BLOCKS = 2048
PAGES_PER_BLOCK = 256
PAGE_SIZE = 8192
MIN_SAFE_BLOCK = 48


@dataclass
class BlockDump:
    blk: int
    pages: List[bytes]  # programmed pages 0..N-1 (each 8192 B)
    udata: List[Tuple[int, int]]  # (u0, u1) per programmed page


@dataclass
class FtlDiscovery:
    diskc_pair: Tuple[int, int]  # (even_blk, odd_blk) for LBN 0x07
    diska_pair: Tuple[int, int]  # (even_blk, odd_blk) for LBN 0x08
    test_blk: int  # confirmed-erased unused block >= 100
    diskc_dumps: Dict[int, BlockDump]
    diska_dumps: Dict[int, BlockDump]
    cfg_pg0: int  # latest physical page index in LBN 0x07 holding lpg=2 (CONFIG.BIN 0x0000..0x3FFF)
    cfg_pg1: int  # latest physical page index in LBN 0x07 holding lpg=3 (CONFIG.BIN 0x4000..0x77FF)
    desktop_sb_offset: int  # byte offset of desktop.apm inside interleaved LBN 0x08 superblock


def _is_erased_page(c: NandClient, row: int) -> bool:
    raw = c.read_raw_page(row, 64)
    return all(b == 0xFF for b in raw)


def _is_factory_bad_block(c: NandClient, blk: int) -> bool:
    # Check OOB byte 0 of page 0 and page 1 (Micron L83A bad-block marker)
    for pg in (0, 1):
        r = c.read_sector(blk * PAGES_PER_BLOCK + pg, 8192, 0, 16, bch=0, udat=0, ecc=0, scram=0)
        if r["rc"] != 0 or not r["data"] or r["data"][0] != 0xFF:
            return True
    return False


def dump_programmed_block(c: NandClient, blk: int) -> BlockDump:
    """Read all contiguous programmed pages (0..N-1) of a block with hardware BCH40 ECC."""
    base_row = blk * PAGES_PER_BLOCK
    pages: List[bytes] = []
    udata: List[Tuple[int, int]] = []
    for pg in range(PAGES_PER_BLOCK):
        if _is_erased_page(c, base_row + pg):
            break
        data, ecc, u0, u1 = c.read_page_verified(base_row + pg)
        if any(e == 0x3F for e in ecc):
            raise RuntimeError(f"Uncorrectable BCH40 ECC error reading blk {blk} pg {pg}: {ecc}")
        pages.append(data)
        udata.append((u0[0], u1[0]))
    return BlockDump(blk=blk, pages=pages, udata=udata)


def _interleave_superblock(even_dump: BlockDump, odd_dump: BlockDump) -> bytearray:
    max_pg = max(len(even_dump.pages), len(odd_dump.pages))
    sb = bytearray(max_pg * 2 * PAGE_SIZE)
    for pg in range(max_pg):
        if pg < len(even_dump.pages):
            sb[(pg * 2) * PAGE_SIZE : (pg * 2 + 1) * PAGE_SIZE] = even_dump.pages[pg]
        if pg < len(odd_dump.pages):
            sb[(pg * 2 + 1) * PAGE_SIZE : (pg * 2 + 2) * PAGE_SIZE] = odd_dump.pages[pg]
    return sb


def discover_ftl(c: NandClient, verbose: bool = True) -> FtlDiscovery:
    """Dynamically locate LBN 0x07 (/mnt/diskc), LBN 0x08 (/mnt/diska), and a test block."""
    lbn_candidates: Dict[int, List[int]] = {7: [], 8: []}
    test_blk: Optional[int] = None

    if verbose:
        print("Scanning 2,048 NAND blocks read-only to locate FTL partitions...", flush=True)

    for blk in range(MIN_SAFE_BLOCK, TOTAL_BLOCKS):
        row0 = blk * PAGES_PER_BLOCK
        raw = c.read_raw_page(row0, 32)
        if all(b == 0xFF for b in raw):
            if test_blk is None and blk >= 100 and not _is_factory_bad_block(c, blk):
                if _is_erased_page(c, row0 + 1):
                    test_blk = blk
            continue

        r = c.read_sector(row0, 0, 8192, 64, bch=2, udat=3, ecc=1, scram=1)
        if r["rc"] != 0 or (r["r4"] & 0x3F) == 0x3F:
            continue
        u0 = r["r6"]
        lbn = (u0 >> 8) & 0xFFFF
        if lbn in lbn_candidates:
            lbn_candidates[lbn].append(blk)

    if test_blk is None:
        raise RuntimeError("Could not find an erased unused NAND block for self-test!")

    def _pick_pair(lbn: int, blks: List[int]) -> Tuple[int, int]:
        blk_set = set(blks)
        pairs = [(b, b + 1) for b in sorted(blks) if b % 2 == 0 and (b + 1) in blk_set]
        if len(pairs) != 1:
            raise RuntimeError(
                f"Expected exactly 1 superblock pair for LBN 0x{lbn:02x}, found {pairs} (raw={blks}). "
                "Aborting safely to avoid modifying an unfamiliar FTL layout."
            )
        return pairs[0]

    diskc_pair = _pick_pair(7, lbn_candidates[7])
    diska_pair = _pick_pair(8, lbn_candidates[8])

    if verbose:
        print(
            f"  Found LBN 0x07 (/mnt/diskc): blocks {diskc_pair[0]}, {diskc_pair[1]}",
            flush=True,
        )
        print(
            f"  Found LBN 0x08 (/mnt/diska): blocks {diska_pair[0]}, {diska_pair[1]}",
            flush=True,
        )
        print(f"  Found erased unused self-test block: {test_blk}", flush=True)

    # Read and verify LBN 0x07 (/mnt/diskc)
    diskc_dumps = {
        diskc_pair[0]: dump_programmed_block(c, diskc_pair[0]),
        diskc_pair[1]: dump_programmed_block(c, diskc_pair[1]),
    }
    de, do = diskc_dumps[diskc_pair[0]], diskc_dumps[diskc_pair[1]]
    if len(de.pages) != len(do.pages) or len(de.pages) == 0:
        raise RuntimeError(
            f"LBN 0x07 page count mismatch: blk {diskc_pair[0]}={len(de.pages)}, blk {diskc_pair[1]}={len(do.pages)}"
        )

    cfg_pg0 = -1
    cfg_pg1 = -1
    for pg in range(len(de.pages)):
        u0_e = de.udata[pg][0]
        u0_o = do.udata[pg][0]
        if u0_e != u0_o:
            raise RuntimeError(f"LBN 0x07 UDATA mismatch at pg {pg}: 0x{u0_e:08x} != 0x{u0_o:08x}")
        lpg = (u0_e >> 24) & 0xFF
        if lpg == 2:
            cfg_pg0 = pg
        elif lpg == 3:
            cfg_pg1 = pg

    if cfg_pg0 < 0 or cfg_pg1 < 0:
        raise RuntimeError(f"Could not locate latest lpg=2/3 CONFIG.BIN pages in LBN 0x07 (got {cfg_pg0}, {cfg_pg1})")

    live_cfg = de.pages[cfg_pg0] + do.pages[cfg_pg0] + de.pages[cfg_pg1] + do.pages[cfg_pg1][:0x1800]
    hdr, calc_sum, entries = parse_config_entries(live_cfg)
    if hdr[1] != calc_sum or "usb_show_u_disk" not in entries:
        raise RuntimeError(
            f"CONFIG.BIN verification failed on LBN 0x07 (hdr_sum=0x{hdr[1]:08x}, calc=0x{calc_sum:08x}, entries={len(entries)})"
        )

    # Read and verify LBN 0x08 (/mnt/diska) and dynamically locate desktop.apm
    diska_dumps = {
        diska_pair[0]: dump_programmed_block(c, diska_pair[0]),
        diska_pair[1]: dump_programmed_block(c, diska_pair[1]),
    }
    sb_diska = _interleave_superblock(diska_dumps[diska_pair[0]], diska_dumps[diska_pair[1]])
    anchor_pos = sb_diska.find(DESKTOP_ANCHOR_BYTES)
    if anchor_pos < DESKTOP_ANCHOR_OFFSET:
        raise RuntimeError("Could not locate desktop.apm code anchor inside LBN 0x08 (/mnt/diska)!")
    desktop_sb_offset = anchor_pos - DESKTOP_ANCHOR_OFFSET
    if sb_diska[desktop_sb_offset : desktop_sb_offset + 4] != b"\x7fELF":
        raise RuntimeError(f"desktop.apm ELF header missing at superblock offset 0x{desktop_sb_offset:x}!")

    if verbose:
        print(
            f"  Verified CONFIG.BIN ({len(entries)} entries, lpg=2 @ pg {cfg_pg0}, lpg=3 @ pg {cfg_pg1})",
            flush=True,
        )
        print(
            f"  Verified desktop.apm ELF at LBN 0x08 superblock offset 0x{desktop_sb_offset:x}",
            flush=True,
        )

    return FtlDiscovery(
        diskc_pair=diskc_pair,
        diska_pair=diska_pair,
        test_blk=test_blk,
        diskc_dumps=diskc_dumps,
        diska_dumps=diska_dumps,
        cfg_pg0=cfg_pg0,
        cfg_pg1=cfg_pg1,
        desktop_sb_offset=desktop_sb_offset,
    )
