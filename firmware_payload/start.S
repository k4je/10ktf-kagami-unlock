    .set mips32r2
    .set noreorder
    .set noat

    .section .init.text,"ax",@progbits
    .globl _start
_start:
    /* Disable CPU interrupts (CP0 Status IE=0) */
    mfc0    $t0, $12, 0
    nop
    nop
    lui     $t1, 0xffff
    ori     $t1, $t1, 0xfffe
    and     $t0, $t0, $t1
    mtc0    $t0, $12, 0
    ehb

    /* Kick hardware watchdog immediately */
    lui     $t0, 0xb001
    ori     $t0, $t0, 0x801c
    lw      $t1, 0($t0)
    nop
    ori     $t1, $t1, 1
    sw      $t1, 0($t0)

    /* Clear .bss */
    la      $t0, __bss_start
    la      $t1, __bss_end
    beq     $t0, $t1, 2f
    nop
1:
    sw      $zero, 0($t0)
    addiu   $t0, $t0, 4
    bne     $t0, $t1, 1b
    nop

2:
    /* Set stack inside proven safe uncached SRAM (0xB4061000..0xB4063000) */
    la      $sp, __stack_top
    addiu   $sp, $sp, -16

    jal     main
    nop

3:
    /* Idle safely while kicking watchdog if main ever returns */
    lui     $t0, 0xb001
    ori     $t0, $t0, 0x801c
    lw      $t1, 0($t0)
    nop
    ori     $t1, $t1, 1
    sw      $t1, 0($t0)
    b       3b
    nop
