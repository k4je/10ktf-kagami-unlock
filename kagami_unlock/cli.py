"""Command-line interface for 10ktf-kagami-unlock."""
from __future__ import annotations

import argparse
import datetime
import json
import pathlib
import sys
import time
from typing import Dict, List, Tuple

from . import __version__
from .adfu import upload_sram_payload, wait_for_adfu
from .ftl import PAGE_SIZE, PAGES_PER_BLOCK, BlockDump, discover_ftl
from .nand_client import NandClient
from .patches import patch_config_bin, patch_desktop_apm_in_superblock
from .usb_transport import PID_ADFU, PID_NORMAL, find_actions_devices


def _save_backup(backup_dir: pathlib.Path, dumps: Dict[int, BlockDump], meta: dict) -> None:
    backup_dir.mkdir(parents=True, exist_ok=True)
    manifest = dict(meta)
    manifest["blocks"] = {}
    for blk, dump in sorted(dumps.items()):
        bin_name = f"blk_{blk}.bin"
        (backup_dir / bin_name).write_bytes(b"".join(dump.pages))
        manifest["blocks"][str(blk)] = {
            "file": bin_name,
            "num_pages": len(dump.pages),
            "udata": [[u0, u1] for (u0, u1) in dump.udata],
        }
    (backup_dir / "backup_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")


def _run_unused_block_selftest(c: NandClient, test_blk: int, sample_page: bytes, sample_u0: int, sample_u1: int) -> None:
    print(f"\n=== Non-Destructive Write/ECC/UDATA Self-Test on Unused Block {test_blk} ===", flush=True)
    row = test_blk * PAGES_PER_BLOCK
    st_e = c.erase_block(test_blk)
    pre = c.read_raw_page(row, 64)
    if not all(b == 0xFF for b in pre):
        raise RuntimeError(f"Self-test block {test_blk} did not erase cleanly!")
    st_p = c.program_page(row, sample_page, sample_u0, sample_u1)
    rb_data, rb_ecc, rb_u0, rb_u1 = c.read_page_verified(row)
    if rb_data != sample_page or any(e == 0x3F for e in rb_ecc) or max(rb_ecc) > 4:
        raise RuntimeError(f"Self-test readback/ECC failed on blk {test_blk}: ecc={rb_ecc}")
    if rb_u0[0] != sample_u0 or rb_u1[0] != sample_u1:
        raise RuntimeError(f"Self-test UDATA mismatch on blk {test_blk}")
    st_c = c.erase_block(test_blk)
    post = c.read_raw_page(row, 64)
    if not all(b == 0xFF for b in post):
        raise RuntimeError(f"Self-test cleanup erase failed on blk {test_blk}")
    print(
        f"  Self-test PASSED (erase=0x{st_e:02x}, prog=0x{st_p:02x}, max_ecc={max(rb_ecc)}, cleaned=0x{st_c:02x}).",
        flush=True,
    )


def _program_and_verify_block(
    c: NandClient, blk: int, pages: List[bytes], udata: List[Tuple[int, int]]
) -> None:
    assert len(pages) == len(udata) and len(pages) > 0
    t0 = time.monotonic()
    base_row = blk * PAGES_PER_BLOCK
    st_e = c.erase_block(blk)
    for pg, (page_bytes, (u0, u1)) in enumerate(zip(pages, udata)):
        c.program_page(base_row + pg, page_bytes, u0, u1)

    max_ecc = 0
    for pg, (expected, (u0, u1)) in enumerate(zip(pages, udata)):
        rb_data, rb_ecc, rb_u0, rb_u1 = c.read_page_verified(base_row + pg)
        if rb_data != expected:
            raise RuntimeError(f"Verification mismatch on blk {blk} pg {pg}!")
        if any(e == 0x3F for e in rb_ecc):
            raise RuntimeError(f"Uncorrectable ECC on blk {blk} pg {pg}: {rb_ecc}")
        if rb_u0[0] != u0 or rb_u1[0] != u1:
            raise RuntimeError(f"UDATA mismatch on blk {blk} pg {pg}!")
        max_ecc = max(max_ecc, max(rb_ecc))

    if len(pages) < PAGES_PER_BLOCK:
        tail = c.read_raw_page(base_row + len(pages), 1024)
        if not all(b == 0xFF for b in tail):
            raise RuntimeError(f"Trailing page {len(pages)} on blk {blk} is not erased!")

    print(
        f"  VERIFIED blk {blk}: {len(pages)}/{len(pages)} pages OK "
        f"(erase=0x{st_e:02x}, max_ecc={max_ecc}/40, {time.monotonic() - t0:.2f}s)",
        flush=True,
    )


def cmd_status(_args: argparse.Namespace) -> int:
    devs = find_actions_devices()
    if not devs:
        print("No Actions/Kagami USB device detected (10d6:1101 or 10d6:10d6).")
        return 1
    for d in devs:
        mode = (
            "Normal / Mass Storage"
            if d.pid == PID_NORMAL
            else "ADFU Boot ROM"
            if d.pid == PID_ADFU
            else "Unknown"
        )
        print(f"Found {d.vid_pid} ({mode}) at {d.usb_path} [speed={d.speed}, backend={d.backend}]")
    return 0


def cmd_backup(args: argparse.Namespace) -> int:
    dev = wait_for_adfu(poll_timeout_s=args.timeout)
    print(f"Uploading SRAM probe payload to {dev.usb_path}...", flush=True)
    upload_sram_payload(dev)

    with NandClient(dev) as c:
        c.ping()
        jedec, _ = c.verify_nand_chip()
        print(f"Verified NAND: SpecTek/Micron PFE13 (JEDEC {jedec[:5].hex()})", flush=True)
        ftl = discover_ftl(c, verbose=True)

        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = pathlib.Path(args.output or f"kagami_backups/{ts}")
        all_dumps = {**ftl.diskc_dumps, **ftl.diska_dumps}
        _save_backup(
            out_dir,
            all_dumps,
            {
                "created_at": ts,
                "jedec": jedec.hex(),
                "diskc_pair": list(ftl.diskc_pair),
                "diska_pair": list(ftl.diska_pair),
            },
        )
        print(f"\nRead-only backup complete! Saved to: {out_dir.resolve()}", flush=True)
        if args.reboot:
            print("Rebooting Kagami into normal mode...", flush=True)
            c.wdt_reboot()
    return 0


def cmd_unlock(args: argparse.Namespace) -> int:
    dev = wait_for_adfu(poll_timeout_s=args.timeout)
    print(f"Uploading SRAM payload to {dev.usb_path}...", flush=True)
    upload_sram_payload(dev)

    with NandClient(dev) as c:
        c.ping()
        jedec, _ = c.verify_nand_chip()
        print(f"Verified NAND: SpecTek/Micron PFE13 (JEDEC {jedec[:5].hex()})", flush=True)
        ftl = discover_ftl(c, verbose=True)

        # 1. Mandatory local backup before ANY writes
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_dir = pathlib.Path(args.backup_dir or f"kagami_backups/{ts}")
        all_dumps = {**ftl.diskc_dumps, **ftl.diska_dumps}
        _save_backup(
            backup_dir,
            all_dumps,
            {
                "created_at": ts,
                "jedec": jedec.hex(),
                "diskc_pair": list(ftl.diskc_pair),
                "diska_pair": list(ftl.diska_pair),
            },
        )
        print(f"\nSaved mandatory pre-patch backup to: {backup_dir.resolve()}", flush=True)

        # 2. Prepare CONFIG.BIN patch (LBN 0x07)
        ce_blk, co_blk = ftl.diskc_pair
        de, do = ftl.diskc_dumps[ce_blk], ftl.diskc_dumps[co_blk]
        orig_cfg = (
            de.pages[ftl.cfg_pg0]
            + do.pages[ftl.cfg_pg0]
            + de.pages[ftl.cfg_pg1]
            + do.pages[ftl.cfg_pg1][:0x1800]
        )
        patched_cfg, cfg_changed = patch_config_bin(orig_cfg, slide_interval_sec=args.slide_interval)
        new_de_pages = list(de.pages)
        new_do_pages = list(do.pages)
        if cfg_changed:
            new_de_pages[ftl.cfg_pg0] = patched_cfg[0x0000:0x2000]
            new_do_pages[ftl.cfg_pg0] = patched_cfg[0x2000:0x4000]
            new_de_pages[ftl.cfg_pg1] = patched_cfg[0x4000:0x6000]
            new_do_pages[ftl.cfg_pg1] = patched_cfg[0x6000:0x7800] + do.pages[ftl.cfg_pg1][0x1800:]

        # 3. Prepare desktop.apm patch (LBN 0x08)
        ae_blk, ao_blk = ftl.diska_pair
        ae, ao = ftl.diska_dumps[ae_blk], ftl.diska_dumps[ao_blk]
        max_pg = max(len(ae.pages), len(ao.pages))
        sb_diska = bytearray(max_pg * 2 * PAGE_SIZE)
        for pg in range(max_pg):
            if pg < len(ae.pages):
                sb_diska[(pg * 2) * PAGE_SIZE : (pg * 2 + 1) * PAGE_SIZE] = ae.pages[pg]
            if pg < len(ao.pages):
                sb_diska[(pg * 2 + 1) * PAGE_SIZE : (pg * 2 + 2) * PAGE_SIZE] = ao.pages[pg]

        desk_changed = patch_desktop_apm_in_superblock(sb_diska, ftl.desktop_sb_offset)

        new_ae_pages = [bytes(sb_diska[(pg * 2) * PAGE_SIZE : (pg * 2 + 1) * PAGE_SIZE]) for pg in range(len(ae.pages))]
        new_ao_pages = [bytes(sb_diska[(pg * 2 + 1) * PAGE_SIZE : (pg * 2 + 2) * PAGE_SIZE]) for pg in range(len(ao.pages))]

        blocks_to_write: List[Tuple[int, List[bytes], List[Tuple[int, int]]]] = []
        if cfg_changed:
            if new_de_pages != de.pages:
                blocks_to_write.append((ce_blk, new_de_pages, de.udata))
            if new_do_pages != do.pages:
                blocks_to_write.append((co_blk, new_do_pages, do.udata))
        if desk_changed:
            if new_ae_pages != ae.pages:
                blocks_to_write.append((ae_blk, new_ae_pages, ae.udata))
            if new_ao_pages != ao.pages:
                blocks_to_write.append((ao_blk, new_ao_pages, ao.udata))

        if not blocks_to_write:
            print("\nKagami is already unlocked with these exact settings! No NAND writes needed.", flush=True)
            c.wdt_reboot()
            return 0

        # 4. Mandatory non-destructive write self-test on unused block
        _run_unused_block_selftest(c, ftl.test_blk, de.pages[ftl.cfg_pg0], de.udata[ftl.cfg_pg0][0], de.udata[ftl.cfg_pg0][1])

        # 5. Program and verify all modified blocks BEFORE rebooting
        print(f"\n=== Applying Patches ({len(blocks_to_write)} NAND block(s)) ===", flush=True)
        for blk, pages, udata in blocks_to_write:
            _program_and_verify_block(c, blk, pages, udata)

        print("\nAll patches verified 100%! Rebooting Kagami into unlocked firmware...", flush=True)
        c.wdt_reboot()
        print("Done! Your Kagami should mount as a 3.66 GiB FAT32 drive ('MediaPlayer') in ~8 seconds.")
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    backup_dir = pathlib.Path(args.backup_dir)
    manifest_path = backup_dir / "backup_manifest.json"
    if not manifest_path.is_file():
        raise SystemExit(f"Backup manifest not found: {manifest_path}")
    manifest = json.loads(manifest_path.read_text())

    dev = wait_for_adfu(poll_timeout_s=args.timeout)
    print(f"Uploading SRAM payload to {dev.usb_path}...", flush=True)
    upload_sram_payload(dev)

    with NandClient(dev) as c:
        c.ping()
        c.verify_nand_chip()
        print(f"\n=== Restoring Backup from {backup_dir.resolve()} ===", flush=True)
        for blk_str, info in sorted(manifest["blocks"].items(), key=lambda kv: int(kv[0])):
            blk = int(blk_str)
            raw = (backup_dir / info["file"]).read_bytes()
            num_pages = int(info["num_pages"])
            assert len(raw) == num_pages * PAGE_SIZE
            pages = [raw[i * PAGE_SIZE : (i + 1) * PAGE_SIZE] for i in range(num_pages)]
            udata = [(int(u0), int(u1)) for (u0, u1) in info["udata"]]
            _program_and_verify_block(c, blk, pages, udata)

        print("\nRestore verified 100%! Rebooting Kagami...", flush=True)
        c.wdt_reboot()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="kagami-unlock",
        description="Safe plug-and-play USB Mass Storage unlocker for the 10KTF Kagami.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_status = sub.add_parser("status", help="Check connected Kagami USB state")
    p_status.set_defaults(func=cmd_status)

    p_backup = sub.add_parser("backup", help="Read-only backup of /mnt/diskc and /mnt/diska NAND blocks")
    p_backup.add_argument("-o", "--output", help="Output backup directory (default: kagami_backups/<timestamp>)")
    p_backup.add_argument("--timeout", type=float, default=60.0, help="Seconds to wait for Kagami USB")
    p_backup.add_argument("--reboot", action="store_true", help="Reboot back to normal mode after backup")
    p_backup.set_defaults(func=cmd_backup)

    p_unlock = sub.add_parser("unlock", help="Back up, self-test, unlock USB Mass Storage + fix desktop.apm, and reboot")
    p_unlock.add_argument("--slide-interval", type=int, default=6, help="Photo slideshow interval in seconds (default: 6)")
    p_unlock.add_argument("--backup-dir", help="Directory to store mandatory pre-patch backup")
    p_unlock.add_argument("--timeout", type=float, default=60.0, help="Seconds to wait for Kagami USB")
    p_unlock.set_defaults(func=cmd_unlock)

    p_restore = sub.add_parser("restore", help="Restore original NAND blocks from a previous backup directory")
    p_restore.add_argument("backup_dir", help="Path to backup directory containing backup_manifest.json")
    p_restore.add_argument("--timeout", type=float, default=60.0, help="Seconds to wait for Kagami USB")
    p_restore.set_defaults(func=cmd_restore)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
