OUTPUT_ARCH(mips)
ENTRY(_start)

MEMORY
{
    IRAM (rwx) : ORIGIN = 0xB4061000, LENGTH = 0x1400
}

SECTIONS
{
    .text 0xB4061000 : {
        KEEP(*(.init.text*))
        *(.text*)
    } > IRAM

    .rodata : ALIGN(4) {
        *(.rodata*)
    } > IRAM

    .data : ALIGN(4) {
        *(.data*)
        *(.sdata*)
    } > IRAM

    .bss (NOLOAD) : ALIGN(4) {
        __bss_start = .;
        *(.sbss*)
        *(.bss*)
        *(COMMON)
        . = ALIGN(4);
        __bss_end = .;
    } > IRAM

    __stack_top = 0xB4062400;

    ASSERT(__bss_end <= 0xB4062180, "Image exceeds 4480B budget before stack")

    /DISCARD/ : {
        *(.note*)
        *(.reginfo)
        *(.MIPS.abiflags)
        *(.pdr)
        *(.gnu.attributes)
        *(.comment)
        *(.eh_frame*)
    }
}
