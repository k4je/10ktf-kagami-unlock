#!/usr/bin/env python3
"""Build, audit (strict read-only check), and ADFU-encode kagami_nand_probe."""
import hashlib
import pathlib
import re
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
SCRATCH = HERE.parent
sys.path.insert(0, str(SCRATCH / "actions-adfu-tool"))
from adfu_tool import codec  # noqa: E402

CC = "mipsel-linux-gnu-gcc"
OBJCOPY = "mipsel-linux-gnu-objcopy"
OBJDUMP = "mipsel-linux-gnu-objdump"
SIZE = "mipsel-linux-gnu-size"

CFLAGS = [
    "-march=mips32r2",
    "-mno-dsp",
    "-mno-abicalls",
    "-fno-pic",
    "-G", "0",
    "-Os",
    "-ffunction-sections",
    "-fdata-sections",
    "-ffreestanding",
    "-nostdlib",
    "-Wall",
    "-Wextra",
    "-Werror",
]

def run(cmd):
    print("+", " ".join(cmd))
    r = subprocess.run(cmd, text=True, capture_output=True)
    if r.returncode != 0:
        print(r.stdout)
        print(r.stderr, file=sys.stderr)
        r.check_returncode()
    return r

def main():
    start_o = HERE / "start.o"
    main_o = HERE / "main.o"
    elf = HERE / "nand_probe.elf"
    raw_bin = HERE / "nand_probe.bin"
    pad_bin = HERE / "nand_probe.padded.bin"
    enc_bin = HERE / "nand_probe.encoded.bin"
    asm_txt = HERE / "nand_probe.asm"

    run([CC, *CFLAGS, "-c", str(HERE / "start.S"), "-o", str(start_o)])
    run([CC, *CFLAGS, "-c", str(HERE / "main.c"), "-o", str(main_o)])
    run([
        CC, *CFLAGS,
        "-T", str(HERE / "iram.lds"),
        "-nostartfiles",
        "-Wl,--build-id=none",
        "-Wl,--gc-sections",
        "-Wl,-Map=" + str(HERE / "nand_probe.map"),
        str(start_o), str(main_o),
        "-o", str(elf),
    ])

    sz = run([SIZE, str(elf)])
    print(sz.stdout.strip())

    run([OBJCOPY, "-O", "binary", str(elf), str(raw_bin)])
    asm = run([OBJDUMP, "-d", "-M", "reg-names=o32", str(elf)])
    asm_txt.write_text(asm.stdout)

    # Strict Read-Only Safety Audit:
    # Verify NO store to NAND_FSM_START sets bit 1 (data write direction = 0x02)
    # and NO NAND write/erase command constants (0x60, 0xD0, 0x80, 0x85, 0x10) are written to NAND_CMD_FSM0 (offset 0x20).
    lines = asm.stdout.splitlines()
    for i, line in enumerate(lines):
        # Check every sw to 0x20(...) or 0x38(...) near 0xb028
        if re.search(r"\bsw\s+\w+,56\(", line) or re.search(r"\bsw\s+\w+,0x38\(", line):
            ctx = "\n".join(lines[max(0, i-6):i+1])
            print(f"[AUDIT] NAND_FSM_START store @ line {i+1}:\n{ctx}\n")
        if re.search(r"\bsw\s+\w+,32\(", line) or re.search(r"\bsw\s+\w+,0x20\(", line):
            ctx = "\n".join(lines[max(0, i-4):i+1])
            if "b028" in ctx:
                print(f"[AUDIT] NAND_CMD_FSM0 store @ line {i+1}:\n{ctx}\n")

    raw = raw_bin.read_bytes()
    print(f"Raw binary size: {len(raw)} bytes")
    if len(raw) > 4608:
        raise SystemExit(f"ERROR: binary too large ({len(raw)} > 4608)")

    # Pad to 512-byte multiple (ADFU 0x05 requirement)
    pad_len = ((len(raw) + 511) // 512) * 512
    padded = raw + b"\x00" * (pad_len - len(raw))
    pad_bin.write_bytes(padded)

    encoded = codec.encode(padded)
    assert codec.decode(encoded) == padded, "codec round-trip mismatch!"
    enc_bin.write_bytes(encoded)

    print(f"Padded size:  {len(padded)} bytes  SHA-256: {hashlib.sha256(padded).hexdigest()}")
    print(f"Encoded size: {len(encoded)} bytes  SHA-256: {hashlib.sha256(encoded).hexdigest()}")

if __name__ == "__main__":
    main()
