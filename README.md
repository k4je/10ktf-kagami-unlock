# 10KTF Kagami Unlock (`10ktf-kagami-unlock`)

Repurpose your physical **10KTF Kagami** display mirror into a **plug-and-play USB photo frame & video loop display**—without opening the enclosure.

Once unlocked, plugging the Kagami into any computer mounts its internal **3.66 GiB FAT32 drive (`MediaPlayer`)**. Drag and drop `1080×1080` photos or videos, safely eject, and unplug—playback starts automatically.

---

## Two-Part Release

1. **Part 1 — `kagami-unlock` CLI Tool (`kagami_unlock/`):**
   - Software-only switch into Actions silicon Boot ROM ADFU mode (`10d6:1101` → `10d6:10d6`)—no disassembly needed.
   - **Dynamic FTL discovery:** Never hardcodes physical NAND block numbers. Scans each unit's 4 GiB NAND read-only to locate `LBN 0x07` (`/mnt/diskc`, `CONFIG.BIN`) and `LBN 0x08` (`/mnt/diska`, `desktop.apm`) safely regardless of factory bad blocks.
   - **Automatic pre-patch backup** + **non-destructive unused-block write/ECC/UDATA self-test** before touching any live partition.
   - Applies **both** patches atomically before rebooting:
     - **`CONFIG.BIN`:** Enables `usb_show_u_disk = 1`, hides empty `usb_show_card_disk = 0`, sets photo slideshow interval (default `6s`), and updates header checksum.
     - **`desktop.apm` (22-byte patch):** Cleanly stops `advert.apm` when plugged into a PC (`SYS_MSG_USB_B_IN`) so `/mnt/disk0` unmounts without freezing, and automatically relaunches `advert.apm` on unplug (`SYS_MSG_USB_B_OUT`).
   - Full hardware **BCH40 ECC + UDATA readback verification** and one-command **`restore`** back to 100% factory stock.

2. **Part 2 — Kagami Circular Photo Cropper (`docs/`):**
   - Zero-dependency static web app (ready for GitHub Pages from `/docs`).
   - Drag and drop photos, pan/zoom inside an interactive circular mirror mask, and batch-export **`1080×1080` baseline 4:2:0 JPEGs** (zipped or individual) so faces fit the round bezel with accurate colors.

---

## Quick Start (Part 1: Unlock CLI)

### 1. Install
**Linux (zero external dependencies — uses native `usbdevfs`):**
```bash
git clone https://github.com/k4je/10ktf-kagami-unlock.git
cd 10ktf-kagami-unlock
pip install -e .
```
Optional non-root USB permission rule on Linux:
```bash
sudo cp udev/99-kagami-usb.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger
```

**macOS / Windows:**
```bash
pip install -e ".[usb]"
```
*(On Windows, if `10d6:10d6` does not open automatically, install the WinUSB driver for `10d6:10d6` via Zadig.)*

### 2. Unlock Your Kagami
Plug your Kagami into a direct USB data port on your computer and run:
```bash
kagami-unlock unlock
```
Optional flags:
- `--slide-interval 6` — photo slideshow interval in seconds (default: `6`)
- `--backup-dir ./my_backup` — custom directory for the mandatory pre-patch backup

Other commands:
```bash
kagami-unlock status                  # Check USB connection state
kagami-unlock backup --reboot         # Read-only backup of /mnt/diskc & /mnt/diska
kagami-unlock restore <backup_dir>    # Restore 100% factory stock from backup
```

### 3. Load Custom Photos & Videos
1. Your Kagami will appear as a **3.66 GiB USB drive (`MediaPlayer`)** containing the original `KagamiEdit_879.mp4` (back it up first if you want to keep it!).
2. Use the included **Circular Photo Cropper** (`docs/index.html` or GitHub Pages) to prep **`1080×1080` baseline 4:2:0 JPEGs**, or convert videos with `ffmpeg`:
   ```bash
   ffmpeg -i input.mp4 \
     -vf "crop=min(iw\,ih):min(iw\,ih),scale=1080:1080:flags=lanczos" \
     -c:v libx264 -profile:v main -level 4.1 -pix_fmt yuv420p -r 24 -b:v 6M -an \
     output_kagami.mp4
   ```
3. **Always safely eject/unmount `MediaPlayer`** before detaching the magnetic USB cable.

---

## Safety & Disclaimer
- Verified on `SY-JL5017_V1.1` mainboards (`Actions ATJ229R2` Boot ROM `0x5009 rev F` + `SpecTek/Micron PFE13` 4 GiB NAND `2c 44 44 4b a9`).
- `kagami-unlock` strictly refuses to write if the Boot ROM ID, NAND JEDEC ID, FTL layout, or `desktop.apm` instructions do not match known verified signatures, and never touches bootloader blocks `0..3` or FTL tables `8..9`.
- Use at your own risk. Always keep your `kagami_backups/` folder safe!

## Rebuilding the MIPS32 SRAM Payload
The pre-built 4,608-byte SRAM payload (`kagami_unlock/payload/nand_probe.encoded.bin`) can be audited or rebuilt from `firmware_payload/` using `mipsel-linux-gnu-gcc`:
```bash
python3 firmware_payload/build.py
```
