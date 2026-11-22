"""Strict, verified patchers for CONFIG.BIN (/mnt/diskc) and desktop.apm (/mnt/diska)."""
from __future__ import annotations

import struct
from typing import Dict, List, Tuple

CONFIG_SIZE = 30720

# Unique 24-byte anchor at desktop.apm file offset 0x2f4 (VMA 0x47c00274: function prologue)
DESKTOP_ANCHOR_OFFSET = 0x2F4
DESKTOP_ANCHOR_BYTES = bytes.fromhex(
    "d8ffbd272000b2af010012241c00b1af1800b0af2400bfaf"
)

# Exact 7-word patch table for desktop.apm (VMA -> file_off = VMA - 0x47c00000 + 0x80)
DESKTOP_PATCHES: List[Tuple[int, int, int, str]] = [
    (0x47C00340, 0x1040003F, 0x1440003F, "beqz -> bnez (kill advert if running on USB_B_IN)"),
    (0x47C003D0, 0x1000FFBA, 0x1000000B, "b 0x47c002bc -> b 0x47c00400 (relaunch advert on USB_B_OUT)"),
    (0x47C003D4, 0xAE20001C, 0x3C0347C0, "sw zero,28(s1) -> lui v1,0x47c0"),
    (0x47C00444, 0x25456564, 0x00002821, "move a1, zero"),
    (0x47C00448, 0x24060007, 0x0DF01430, "jal 0x47c050c0 (kill_app)"),
    (0x47C0044C, 0x0DF01454, 0x24060001, "li a2, 1"),
    (0x47C00450, 0x24070001, 0x00000000, "nop"),
]


def parse_config_entries(data: bytes) -> Tuple[Tuple[int, ...], int, Dict[str, dict]]:
    """Parse Actions CONFIG.BIN hash table and compute checksum."""
    if len(data) != CONFIG_SIZE:
        raise ValueError(f"Unexpected CONFIG.BIN length: {len(data)} (expected {CONFIG_SIZE})")
    hdr = struct.unpack_from("<4I", data, 0)
    calc_sum = sum(data[16:CONFIG_SIZE]) & 0xFFFFFFFF
    entries: Dict[str, dict] = {}
    seen = set()
    for b_off in range(16, 0x1FF0, 4):
        p = struct.unpack_from("<I", data, b_off)[0]
        while p != 0 and p not in seen and 0x1FF0 <= p < len(data) - 16:
            seen.add(p)
            nxt, klen = struct.unpack_from("<2I", data, p)
            if klen == 0 or p + 8 + klen + 12 > len(data):
                break
            key = data[p + 8 : p + 8 + klen].rstrip(b"\x00").decode("ascii", errors="replace")
            vlen, vcap, voff = struct.unpack_from("<3I", data, p + 8 + klen)
            entries[key] = {
                "p": p,
                "klen": klen,
                "vlen": vlen,
                "vcap": vcap,
                "voff": voff,
                "raw_v": data[voff : voff + vcap],
            }
            p = nxt
    return hdr, calc_sum, entries


def patch_config_bin(orig_cfg: bytes, slide_interval_sec: int = 6) -> Tuple[bytes, bool]:
    """Patch CONFIG.BIN to enable usb_show_u_disk and set photo slideshow interval.

    Returns (patched_bytes, changed).
    """
    hdr0, sum0, entries = parse_config_entries(orig_cfg)
    if hdr0[1] != sum0:
        raise ValueError(f"Original CONFIG.BIN checksum invalid: 0x{hdr0[1]:08x} != 0x{sum0:08x}")

    desired = {
        "usb_show_card_disk": 0,
        "usb_show_u_disk": 1,
        "CFG_PHOTO_PLAY_INTERVAL": slide_interval_sec * 1000,
        "photo_slide_interval": slide_interval_sec,
    }

    patched = bytearray(orig_cfg)
    changed = False
    for name, new_val in desired.items():
        if name not in entries:
            raise ValueError(f"Required CONFIG.BIN key missing: {name}")
        e = entries[name]
        if e["vlen"] != 4 or e["vcap"] != 4:
            raise ValueError(f"Unexpected entry layout for {name}: {e}")
        cur_val = struct.unpack_from("<I", patched, e["voff"])[0]
        if cur_val != new_val:
            struct.pack_into("<I", patched, e["voff"], new_val)
            changed = True

    new_sum = sum(patched[16:CONFIG_SIZE]) & 0xFFFFFFFF
    struct.pack_into("<I", patched, 4, new_sum)
    patched_bytes = bytes(patched)

    hdr1, sum1, _ = parse_config_entries(patched_bytes)
    assert hdr1[1] == sum1 == new_sum
    return patched_bytes, changed


def patch_desktop_apm_in_superblock(sb: bytearray, desktop_sb_offset: int) -> bool:
    """Verify and patch desktop.apm in-place inside an interleaved LBN 0x08 superblock.

    Returns True if patched, False if already patched. Raises ValueError if firmware differs.
    """
    already_patched = True
    matches_stock = True
    for vma, orig_word, new_word, _ in DESKTOP_PATCHES:
        foff = vma - 0x47C00000 + 0x80
        cur = struct.unpack_from("<I", sb, desktop_sb_offset + foff)[0]
        if cur != orig_word:
            matches_stock = False
        if cur != new_word:
            already_patched = False

    if already_patched:
        return False
    if not matches_stock:
        raise ValueError(
            "desktop.apm instructions do not match known 10KTF Kagami firmware! "
            "Refusing to patch an unfamiliar firmware revision."
        )

    for vma, orig_word, new_word, _ in DESKTOP_PATCHES:
        foff = vma - 0x47C00000 + 0x80
        struct.pack_into("<I", sb, desktop_sb_offset + foff, new_word)
    return True
