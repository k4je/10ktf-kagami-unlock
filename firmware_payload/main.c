/*
 * Interactive NAND reader & safe partition updater for Actions ATJ229R2 (Kagami).
 *
 * HARDWARE SAFETY GUARD:
 *   Erase and program operations require UNLOCK_MAGIC (0xA55A1936) AND strictly reject
 *   any block < 48 (row < 12288), protecting Bootloader (Blocks 0..3) and FTL tables (Block 8).
 *
 * Memory map (all uncached kseg1 SRAM):
 *   0xB4060000..0xB40607FF : BootROM state (untouched; reads HS flag at 0xB406002C)
 *   0xB4061000..0xB4061FFF : Code + rodata + data + bss (<= 4 KiB)
 *   0xB4062000..0xB40623FF : Stack (1 KiB, sp starts at 0xB4062400)
 *   0xB4062400..0xB4062BFF : Probe report / USB TX buffer (2 KiB)
 *   0xB4062C00..0xB4062FFF : NAND DMA sector scratch buffer (1 KiB)
 */

#include <stdint.h>

#define REG8(a)   (*(volatile uint8_t  *)(uintptr_t)(a))
#define REG16(a)  (*(volatile uint16_t *)(uintptr_t)(a))
#define REG32(a)  (*(volatile uint32_t *)(uintptr_t)(a))

/* PMU / CMU / RTC */
#define PMU_REG_58      0xB0000058U
#define CMU_COREPLL     0xB0010000U
#define CMU_NANDPLL     0xB001002CU
#define CMU_NANDCLK     0xB0010034U
#define CMU_DEVCLKEN    0xB0010080U
#define CMU_DEVRST      0xB0010088U
#define RTC_WDCTL       0xB001801CU

/* GPIO / MFP (exact ATJ229R2 BootROM 0xbfc056c4 addresses) */
#define MFP_CTL3        0xB01C0024U
#define GPIO_REG_2C     0xB01C002CU
#define PAD_DRV0        0xB01C0088U

/* DMA Ch1 (exact BootROM 0xbfc05a94 addresses) */
#define DMA_CTL         0xB0220000U
#define DMA_IRQEN       0xB0220004U
#define DMA_IRQPD       0xB0220008U
#define DMACH1_MODE     0xB0220130U
#define DMACH1_SRC      0xB0220134U
#define DMACH1_DST      0xB0220138U
#define DMACH1_CNT      0xB022013CU
#define DMACH1_REM      0xB0220140U
#define DMACH1_CMD      0xB0220144U

/* NAND controller */
#define NAND_CTL        0xB0280000U
#define NAND_STAT       0xB0280004U
#define NAND_CFG        0xB0280008U
#define NAND_BC         0xB028000CU
#define NAND_DATA       0xB0280010U
#define NAND_COLADDR    0xB0280014U
#define NAND_ROWADDR0   0xB0280018U
#define NAND_ROWADDR1   0xB028001CU
#define NAND_CMD_FSM0   0xB0280020U
#define NAND_CMD_FSM_CTL0 0xB0280024U
#define NAND_CMD_FSM1   0xB0280028U
#define NAND_CMD_FSM_CTL1 0xB028002CU
#define NAND_FSM_START  0xB0280038U
#define NAND_ECCST0     0xB028003CU
#define NAND_ECCST1     0xB0280040U
#define NAND_ECCST2     0xB0280044U
#define NAND_ECCST3     0xB0280048U
#define NAND_UDATAC0    0xB028004CU
#define NAND_UDATAC1    0xB0280050U
#define NAND_UDATAC2    0xB0280054U
#define NAND_UDATAC3    0xB0280058U
#define NAND_REG_5C     0xB028005CU

/* USB OTG FIFO/EP registers (already enumerated by BootROM) */
#define USB_OUT1BCL     0xB0290008U
#define USB_OUT1BCH     0xB0290009U
#define USB_OUT1CS      0xB029000BU
#define USB_IN2BCL      0xB0290014U
#define USB_IN2BCH      0xB0290015U
#define USB_IN2CS       0xB0290017U
#define USB_FIFO1DATA   0xB0290084U
#define USB_FIFO2DATA   0xB0290088U
#define BROM_HS_FLAG    0xB406002CU

/* Fixed uncached SRAM buffers */
#define REPORT_BUF_ADDR  0xB4062400U /* 2048 bytes */
#define DMA_SCRATCH_ADDR 0xB4062C00U /* 1024 bytes */

#define UNLOCK_MAGIC     0xA55A1936U
#define MIN_WRITABLE_BLK 48U
#define MIN_WRITABLE_ROW (MIN_WRITABLE_BLK * 256U)

static uint32_t g_max_pkt = 512U;

static inline void wdt_kick(void)
{
    REG32(RTC_WDCTL) |= 1U;
}

static void delay_loops(uint32_t n)
{
    while (n--) {
        __asm__ volatile ("nop");
    }
}

static void mem_zero32(uint32_t *dst, uint32_t words)
{
    while (words--) {
        *dst++ = 0U;
    }
}

static void mem_copy32(uint32_t *dst, const uint32_t *src, uint32_t words)
{
    while (words--) {
        *dst++ = *src++;
    }
}

/* ---------------- USB Bulk EP2 IN / EP1 OUT ---------------- */

static int usb_in2_wait_ready(void)
{
    uint32_t t = 0x00400000U;
    while ((REG8(USB_IN2CS) & 2U) != 0U) {
        wdt_kick();
        if (--t == 0U) {
            return -1;
        }
    }
    return 0;
}

static int usb_send_packet(const uint8_t *buf, uint32_t len)
{
    if (usb_in2_wait_ready() < 0) {
        return -1;
    }
    uint32_t i = 0U;
    while (i + 4U <= len) {
        uint32_t w = (uint32_t)buf[i]
                   | ((uint32_t)buf[i + 1U] << 8)
                   | ((uint32_t)buf[i + 2U] << 16)
                   | ((uint32_t)buf[i + 3U] << 24);
        REG32(USB_FIFO2DATA) = w;
        i += 4U;
    }
    if (i < len) {
        uint32_t w = 0U;
        uint32_t shift = 0U;
        while (i < len) {
            w |= ((uint32_t)buf[i++] << shift);
            shift += 8U;
        }
        REG32(USB_FIFO2DATA) = w;
    }
    REG8(USB_IN2BCL) = (uint8_t)(len & 0xFFU);
    REG8(USB_IN2BCH) = (uint8_t)((len >> 8) & 0xFFU);
    REG8(USB_IN2CS) = 0U;
    return usb_in2_wait_ready();
}

static int usb_send_stream(const uint8_t *buf, uint32_t len)
{
    uint32_t off = 0U;
    while (off < len) {
        uint32_t chunk = len - off;
        if (chunk > g_max_pkt) {
            chunk = g_max_pkt;
        }
        if (usb_send_packet(buf + off, chunk) < 0) {
            return -1;
        }
        off += chunk;
    }
    return 0;
}

static uint32_t usb_recv_cmd(uint32_t *out_words, uint32_t max_words)
{
    REG8(USB_OUT1CS) = 0U;
    while ((REG8(USB_OUT1CS) & 2U) != 0U) {
        wdt_kick();
    }
    uint32_t bc = (uint32_t)REG8(USB_OUT1BCL) | ((uint32_t)REG8(USB_OUT1BCH) << 8);
    uint32_t words = (bc + 3U) >> 2;
    for (uint32_t i = 0U; i < words; i++) {
        uint32_t w = REG32(USB_FIFO1DATA);
        if (i < max_words) {
            out_words[i] = w;
        }
    }
    return bc;
}

/* ---------------- NAND Primitives ---------------- */

static int nand_wait_rb_ready(uint32_t loops)
{
    while (loops--) {
        wdt_kick();
        if ((REG32(NAND_STAT) & 0x80000000U) == 0U) {
            return 0;
        }
    }
    return -1;
}

static int nand_wait_fsm_ready(uint32_t loops)
{
    while (loops--) {
        wdt_kick();
        if ((REG32(NAND_STAT) & 0x08U) != 0U) {
            return 0;
        }
    }
    return -1;
}

static int nand_wait_data_ready(uint32_t loops)
{
    while (loops--) {
        wdt_kick();
        if ((REG32(NAND_STAT) & 0x10U) != 0U) {
            return 0;
        }
    }
    return -1;
}

static void nand_soft_recover(void)
{
    REG32(DMACH1_CMD) = 0U;
    REG32(NAND_CTL) &= ~1U;
    delay_loops(16U);
    REG32(NAND_CTL) |= 1U;
    (void)nand_wait_rb_ready(50000U);
    REG32(NAND_CTL) |= 1U;
}

static void nand_init_brom(void)
{
    REG32(CMU_NANDCLK) = 0U;
    REG32(CMU_DEVCLKEN) |= 0x04000310U;
    REG32(PAD_DRV0) = (REG32(PAD_DRV0) & 0xFFFF0000U) | 0xA999U;
    REG32(GPIO_REG_2C) = (REG32(GPIO_REG_2C) & 0xFFFF1FFFU) | 0xA000U;

    REG32(CMU_DEVRST) &= ~0x20U;
    delay_loops(64U);
    REG32(CMU_DEVRST) |= 0x20U;
    delay_loops(64U);

    REG32(MFP_CTL3) = (REG32(MFP_CTL3) & 0xF00FFFFFU) | 0x08000000U;

    REG32(NAND_CTL) &= ~1U;
    REG32(NAND_CTL) |= 1U;
    (void)nand_wait_rb_ready(50000U);
    REG32(NAND_CTL) |= 1U;

    REG32(NAND_FSM_START) &= ~4U;
    REG32(NAND_UDATAC0) = 0xFFFFFFFFU;
    REG32(NAND_UDATAC1) = 0xFFFFFFFFU;
    REG32(NAND_UDATAC2) = 0xFFFFFFFFU;
    REG32(NAND_UDATAC3) = 0xFFFFFFFFU;
}

static void nand_select_ce(uint32_t ce)
{
    uint32_t ctl = REG32(NAND_CTL) & ~0x78U;
    ctl |= (1U << (3U + (ce & 3U)));
    REG32(NAND_CTL) = ctl;
}

static void nand_set_mode(uint32_t mode)
{
    uint32_t ctl = REG32(NAND_CTL);
    if (mode == 2U) {
        REG32(NAND_REG_5C) = 0x840148C9U;
        ctl |= 4U;
    } else {
        ctl &= ~4U;
    }
    ctl = (ctl & ~0x3000U) | ((mode & 3U) << 12);
    REG32(NAND_CTL) = ctl;
}

static void nand_dma_setup_read(uint32_t dst_addr, uint32_t len)
{
    REG32(DMA_CTL) = 2U;
    REG32(DMA_IRQEN) = 0U;
    REG32(DMA_IRQPD) = 0xFFFFFFFFU;
    REG32(DMACH1_MODE) = ((dst_addr & 0x10000000U) != 0U) ? 0x00040054U : 0x00120054U;
    REG32(DMACH1_SRC) = NAND_DATA & 0x1FFFFFFFU;
    REG32(DMACH1_DST) = dst_addr & 0x1FFFFFFFU;
    REG32(DMACH1_CNT) = len;
}

static void nand_dma_setup_write(uint32_t src_addr, uint32_t len)
{
    REG32(DMA_CTL) = 2U;
    REG32(DMA_IRQEN) = 0U;
    REG32(DMA_IRQPD) = 0xFFFFFFFFU;
    REG32(DMACH1_MODE) = ((src_addr & 0x10000000U) != 0U) ? 0x00540004U : 0x00540012U;
    REG32(DMACH1_SRC) = src_addr & 0x1FFFFFFFU;
    REG32(DMACH1_DST) = NAND_DATA & 0x1FFFFFFFU;
    REG32(DMACH1_CNT) = len;
}

static int nand_dma_wait_done(uint32_t loops)
{
    while (loops--) {
        wdt_kick();
        if ((REG32(DMACH1_CMD) & 1U) == 0U) {
            return 0;
        }
    }
    REG32(DMACH1_CMD) = 0U;
    return -1;
}

static int nand_reset(void)
{
    (void)nand_wait_fsm_ready(20000U);
    REG32(NAND_CMD_FSM0) = 0xFFU;
    REG32(NAND_CMD_FSM_CTL0) = 0x60U;
    REG32(NAND_CMD_FSM1) = 0U;
    REG32(NAND_CMD_FSM_CTL1) = 0U;
    REG32(NAND_FSM_START) = 1U;
    delay_loops(100U);
    int rc = nand_wait_rb_ready(200000U);
    (void)nand_wait_fsm_ready(20000U);
    return rc;
}

static uint32_t nand_read_status(void)
{
    (void)nand_wait_fsm_ready(20000U);
    REG32(NAND_COLADDR) = 0U;
    REG32(NAND_ROWADDR0) = 0U;
    REG32(NAND_ROWADDR1) = 0U;
    REG32(NAND_CFG) &= ~0x70U;
    REG32(NAND_BC) = 1U;
    REG32(NAND_CMD_FSM0) = 0x70U;
    REG32(NAND_CMD_FSM_CTL0) = 0x61U;
    REG32(NAND_CMD_FSM1) = 0U;
    REG32(NAND_CMD_FSM_CTL1) = 0U;
    REG32(NAND_FSM_START) = 1U;
    if (nand_wait_data_ready(20000U) < 0) {
        return 0xFFFFFFFFU;
    }
    return REG32(NAND_DATA) & 0xFFU;
}

static int nand_read_id(uint8_t addr, uint8_t *out, uint32_t len)
{
    (void)nand_wait_fsm_ready(20000U);
    REG32(NAND_COLADDR) = 0U;
    REG32(NAND_ROWADDR0) = (uint32_t)addr;
    REG32(NAND_ROWADDR1) = 0U;
    REG32(NAND_CFG) &= ~0x70U;
    REG32(NAND_BC) = len;
    REG32(NAND_CMD_FSM0) = 0x90U;
    REG32(NAND_CMD_FSM_CTL0) = 0x69U;
    REG32(NAND_CMD_FSM1) = 0U;
    REG32(NAND_CMD_FSM_CTL1) = 0U;
    REG32(NAND_FSM_START) = 1U;
    if (nand_wait_data_ready(20000U) < 0) {
        return -1;
    }
    uint32_t got = 0U;
    while (got < len) {
        uint32_t w = REG32(NAND_DATA);
        for (uint32_t b = 0U; b < 4U && got < len; b++) {
            out[got++] = (uint8_t)(w >> (b * 8U));
        }
    }
    (void)nand_wait_fsm_ready(20000U);
    return 0;
}

static int nand_read_chunk(uint32_t row, uint32_t col_data, uint32_t col_oob,
                           uint32_t dst_addr, uint32_t len, uint32_t flags)
{
    uint32_t bch     = flags & 3U;
    uint32_t udat    = (flags >> 2) & 3U;
    uint32_t ecc_en  = (flags >> 4) & 1U;
    uint32_t scram   = (flags >> 5) & 1U;
    uint32_t skip_rd = (flags >> 6) & 1U;
    uint32_t cmd_a2  = (flags >> 7) & 1U;

    (void)nand_wait_fsm_ready(20000U);
    REG32(NAND_ROWADDR0) = row;
    REG32(NAND_ROWADDR1) = 0U;
    REG32(NAND_COLADDR) = (col_data & 0xFFFFU) | ((col_oob & 0xFFFFU) << 16);

    uint32_t cfg = (REG32(NAND_CFG) & 0xFFFFFC8CU)
                 | bch
                 | (2U << 4)
                 | (udat << 8);
    REG32(NAND_CFG) = cfg;

    nand_dma_setup_read(dst_addr, len);
    REG32(NAND_BC) = len;

    if (!skip_rd) {
        if (cmd_a2) {
            REG32(NAND_CMD_FSM0) = 0x003000A2U;
            REG32(NAND_CMD_FSM_CTL0) = 0x00626C60U;
        } else {
            REG32(NAND_CMD_FSM0) = 0x00003000U;
            REG32(NAND_CMD_FSM_CTL0) = 0x0000626CU;
        }
        REG32(NAND_CMD_FSM1) = 0U;
        REG32(NAND_CMD_FSM_CTL1) = 0U;
        REG32(NAND_FSM_START) = 1U;
        (void)REG32(NAND_FSM_START);
        if (nand_wait_fsm_ready(50000U) < 0) {
            nand_soft_recover();
            return -2;
        }
        if (nand_wait_rb_ready(200000U) < 0) {
            nand_soft_recover();
            return -3;
        }
    }

    if (ecc_en) {
        REG32(NAND_UDATAC0) = 0xFFFFFFFFU;
        REG32(NAND_UDATAC1) = 0xFFFFFFFFU;
        REG32(NAND_UDATAC2) = 0xFFFFFFFFU;
        REG32(NAND_UDATAC3) = 0xFFFFFFFFU;
        REG32(NAND_CMD_FSM0) = 0xE005E005U;
        REG32(NAND_CMD_FSM_CTL0) = cmd_a2 ? 0x717C616CU : 0x71746164U;
    } else {
        REG32(NAND_CMD_FSM0) = 0x0000E005U;
        REG32(NAND_CMD_FSM_CTL0) = 0x00006164U;
    }
    REG32(NAND_CMD_FSM1) = 0U;
    REG32(NAND_CMD_FSM_CTL1) = 0U;

    uint32_t start = 1U | (ecc_en ? 4U : 0U) | (scram ? 0x18U : 0U);
    REG32(NAND_FSM_START) = start;
    (void)REG32(NAND_FSM_START);

    REG32(DMACH1_CMD) = 1U;
    if (nand_dma_wait_done(300000U) < 0) {
        nand_soft_recover();
        return -4;
    }
    if (nand_wait_rb_ready(100000U) < 0) {
        nand_soft_recover();
        return -5;
    }
    return 0;
}

/* ---------------- Safe NAND Erase & Program Primitives ---------------- */

static int nand_erase_block(uint32_t blk, uint32_t *out_status)
{
    if (blk < MIN_WRITABLE_BLK || blk >= 2048U) {
        return -100;
    }
    (void)nand_wait_fsm_ready(50000U);
    REG32(NAND_ROWADDR0) = blk * 256U;
    REG32(NAND_ROWADDR1) = 0U;
    REG32(NAND_COLADDR)  = 0U;
    REG32(NAND_CFG) = (REG32(NAND_CFG) & ~0x70U) | (2U << 4); /* 3 row address bytes */

    /* Block erase 60h (3 row addr cycles) -> D0h */
    REG32(NAND_CMD_FSM0) = 0x0000D060U;
    REG32(NAND_CMD_FSM_CTL0) = 0x00006068U;
    REG32(NAND_CMD_FSM1) = 0U;
    REG32(NAND_CMD_FSM_CTL1) = 0U;
    REG32(NAND_FSM_START) = 1U;
    (void)REG32(NAND_FSM_START);

    delay_loops(200U);
    if (nand_wait_fsm_ready(200000U) < 0) {
        nand_soft_recover();
        return -2;
    }
    if (nand_wait_rb_ready(1000000U) < 0) {
        nand_soft_recover();
        return -3;
    }
    uint32_t st = nand_read_status();
    *out_status = st;
    if (st == 0xFFFFFFFFU || (st & 0x01U) != 0U) {
        return -4;
    }
    return 0;
}

static int nand_prog_page_start(uint32_t row)
{
    if (row < MIN_WRITABLE_ROW || row >= (2048U * 256U)) {
        return -100;
    }
    (void)nand_wait_fsm_ready(50000U);
    REG32(NAND_ROWADDR0) = row;
    REG32(NAND_ROWADDR1) = 0U;
    REG32(NAND_COLADDR)  = 0U;
    REG32(NAND_CFG) = (REG32(NAND_CFG) & ~0x70U) | (2U << 4); /* 3 row address bytes */

    /* Serial data input 80h (C1 C2 R1 R2 R3) */
    REG32(NAND_CMD_FSM0) = 0x00000080U;
    REG32(NAND_CMD_FSM_CTL0) = 0x0000006CU;
    REG32(NAND_CMD_FSM1) = 0U;
    REG32(NAND_CMD_FSM_CTL1) = 0U;
    REG32(NAND_FSM_START) = 1U;
    (void)REG32(NAND_FSM_START);

    if (nand_wait_fsm_ready(50000U) < 0) {
        nand_soft_recover();
        return -2;
    }
    if (nand_wait_rb_ready(200000U) < 0) {
        nand_soft_recover();
        return -3;
    }
    return 0;
}

static int nand_prog_sector(uint32_t col_pair, uint32_t u0, uint32_t u1, uint32_t flags)
{
    uint32_t bch    = flags & 3U;
    uint32_t udat   = (flags >> 2) & 3U;
    uint32_t ecc_en = (flags >> 4) & 1U;
    uint32_t scram  = (flags >> 5) & 1U;

    (void)nand_wait_fsm_ready(50000U);
    REG32(NAND_COLADDR) = col_pair;
    REG32(NAND_UDATAC0) = u0;
    REG32(NAND_UDATAC1) = u1;
    REG32(NAND_UDATAC2) = 0xFFFFFFFFU;
    REG32(NAND_UDATAC3) = 0xFFFFFFFFU;

    uint32_t cfg = (REG32(NAND_CFG) & 0xFFFFFC8CU)
                 | bch
                 | (2U << 4)
                 | (udat << 8);
    REG32(NAND_CFG) = cfg;

    nand_dma_setup_write(DMA_SCRATCH_ADDR, 1024U);
    REG32(NAND_BC) = 1024U;

    if (ecc_en) {
        REG32(NAND_CMD_FSM0) = 0x00008585U;
        REG32(NAND_CMD_FSM_CTL0) = 0x00007565U;
    } else {
        REG32(NAND_CMD_FSM0) = 0x00000085U;
        REG32(NAND_CMD_FSM_CTL0) = 0x00000065U;
    }
    REG32(NAND_CMD_FSM1) = 0U;
    REG32(NAND_CMD_FSM_CTL1) = 0U;

    uint32_t start = 1U | 2U /* WRITE */ | (ecc_en ? 4U : 0U) | (scram ? 0x18U : 0U);
    REG32(NAND_FSM_START) = start;
    (void)REG32(NAND_FSM_START);

    REG32(DMACH1_CMD) = 1U;
    if (nand_dma_wait_done(300000U) < 0) {
        nand_soft_recover();
        return -4;
    }
    if (nand_wait_fsm_ready(100000U) < 0) {
        nand_soft_recover();
        return -5;
    }
    if (nand_wait_rb_ready(100000U) < 0) {
        nand_soft_recover();
        return -6;
    }
    return 0;
}

static int nand_prog_page_finish(uint32_t *out_status)
{
    (void)nand_wait_fsm_ready(50000U);
    REG32(NAND_CMD_FSM0) = 0x00000010U;
    REG32(NAND_CMD_FSM_CTL0) = 0x00000060U;
    REG32(NAND_CMD_FSM1) = 0U;
    REG32(NAND_CMD_FSM_CTL1) = 0U;
    REG32(NAND_FSM_START) = 1U;
    (void)REG32(NAND_FSM_START);

    delay_loops(200U);
    if (nand_wait_fsm_ready(200000U) < 0) {
        nand_soft_recover();
        return -2;
    }
    if (nand_wait_rb_ready(1000000U) < 0) {
        nand_soft_recover();
        return -3;
    }
    uint32_t st = nand_read_status();
    *out_status = st;
    if (st == 0xFFFFFFFFU || (st & 0x01U) != 0U) {
        return -4;
    }
    return 0;
}

/* ---------------- Report Layout (2048 bytes @ 0xB4062400) ---------------- */

typedef struct {
    uint8_t  magic[8];          /* "KAGNAND1" */
    uint32_t hs_flag;
    uint32_t prid;
    uint32_t wdctl_pre;
    uint32_t cmu_corepll;
    uint32_t cmu_nandpll;
    uint32_t cmu_nandclk;
    uint32_t cmu_devclken;
    uint32_t cmu_devrst;
    uint32_t pmu_reg58;
    uint32_t mfp_ctl3;
    uint32_t gpio_reg2c;
    uint32_t pad_drv0;
    uint32_t nand_ctl_pre;
    uint32_t nand_stat_pre;
    uint32_t nand_reg5c_pre;
    uint32_t usb_cs_pre;

    uint32_t nand_ctl_post;
    uint32_t nand_stat_post;
    uint32_t reset_rc;
    uint32_t status_byte;
    uint8_t  id_ce0[8];
    uint8_t  id_ce1[8];
    uint8_t  onfi_sig[4];
    uint32_t id_rc;
    uint32_t onfi_rc;
    uint32_t raw0_rc;

    uint32_t mbrec_found;
    uint32_t mbrec_page_sz;
    uint32_t mbrec_bch;
    uint32_t mbrec_scram;
    uint32_t mbrec_a2;
    uint32_t mbrec_eccst[4];
    uint32_t mbrec_udat[2];
} probe_hdr_t;

static void run_initial_probe(void)
{
    uint8_t *rep = (uint8_t *)REPORT_BUF_ADDR;
    mem_zero32((uint32_t *)REPORT_BUF_ADDR, 2048U / 4U);

    probe_hdr_t *h = (probe_hdr_t *)REPORT_BUF_ADDR;
    h->magic[0] = 'K'; h->magic[1] = 'A'; h->magic[2] = 'G'; h->magic[3] = 'N';
    h->magic[4] = 'A'; h->magic[5] = 'N'; h->magic[6] = 'D'; h->magic[7] = '1';

    nand_init_brom();
    nand_select_ce(0U);
    h->reset_rc    = (uint32_t)nand_reset();
    h->status_byte = nand_read_status();

    (void)usb_send_stream(rep, 2048U);
}

/* ---------------- Interactive Command Loop ---------------- */

#define CMD_MAGIC           0x444D434BU /* "KCMD" */
#define RSP_MAGIC           0x5053524BU /* "KRSP" */

#define OP_PING             1U
#define OP_READ_RAW         2U
#define OP_READ_SECTOR      3U
#define OP_READ_ID          4U
#define OP_READ_MEM         5U
#define OP_SET_MODE         6U
#define OP_LOAD_CHUNK       7U
#define OP_ERASE_BLOCK      8U
#define OP_PROG_PAGE_START  9U
#define OP_PROG_SECTOR      10U
#define OP_PROG_PAGE_FINISH 11U
#define OP_NAND_RESET       14U
#define OP_WDT_REBOOT       15U

static void command_loop(void)
{
    uint32_t cmd[72]; /* 8 header words (32B) + up to 64 payload words (256B) */
    uint32_t *tx = (uint32_t *)REPORT_BUF_ADDR;

    for (;;) {
        uint32_t bc = usb_recv_cmd(cmd, 72U);
        if (bc < 32U || cmd[0] != CMD_MAGIC) {
            continue;
        }
        uint32_t op   = cmd[1];
        uint32_t arg0 = cmd[2];
        uint32_t arg1 = cmd[3];
        uint32_t arg2 = cmd[4];
        uint32_t arg3 = cmd[5];
        uint32_t arg4 = cmd[6];
        uint32_t seq  = cmd[7];

        tx[0] = RSP_MAGIC;
        tx[1] = seq;
        tx[2] = 0U;
        tx[3] = 0U;
        tx[4] = REG32(NAND_STAT);
        tx[5] = REG32(NAND_CTL);
        tx[6] = REG32(RTC_WDCTL);
        tx[7] = g_max_pkt;

        uint32_t data_len = 0U;

        if (op == OP_PING) {
            data_len = 32U;
            tx[8]  = REG32(CMU_COREPLL);
            tx[9]  = REG32(CMU_NANDPLL);
            tx[10] = REG32(CMU_NANDCLK);
            tx[11] = REG32(NAND_CFG);
            tx[12] = REG32(NAND_ECCST0);
            tx[13] = REG32(NAND_ECCST1);
            tx[14] = REG32(NAND_UDATAC0);
            tx[15] = REG32(NAND_UDATAC1);
        } else if (op == OP_READ_RAW || op == OP_READ_SECTOR) {
            uint32_t ce = (arg4 >> 8) & 3U;
            uint32_t len = arg3;
            if (len == 0U || len > 1024U) {
                tx[2] = 0xFFFFFFF0U;
            } else {
                nand_select_ce(ce);
                uint32_t flags = (op == OP_READ_RAW) ? (arg4 & 0xC0U) : (arg4 & 0xFFU);
                int rc = nand_read_chunk(arg0, arg1, arg2, DMA_SCRATCH_ADDR, len, flags);
                tx[2] = (uint32_t)rc;
                tx[4] = REG32(NAND_ECCST0);
                tx[5] = REG32(NAND_ECCST1);
                tx[6] = REG32(NAND_UDATAC0);
                tx[7] = REG32(NAND_UDATAC1);
                if (rc == 0) {
                    data_len = len;
                    mem_copy32(tx + 8U, (const uint32_t *)DMA_SCRATCH_ADDR, (len + 3U) >> 2);
                }
            }
        } else if (op == OP_READ_ID) {
            uint32_t len = arg1;
            if (len == 0U || len > 64U) {
                tx[2] = 0xFFFFFFF0U;
            } else {
                nand_select_ce(arg2 & 3U);
                int rc = nand_read_id((uint8_t)(arg0 & 0xFFU), (uint8_t *)(tx + 8U), len);
                tx[2] = (uint32_t)rc;
                if (rc == 0) {
                    data_len = len;
                }
            }
        } else if (op == OP_READ_MEM) {
            uint32_t len = arg1 & ~3U;
            if ((arg0 & 3U) != 0U || len == 0U || len > 1024U) {
                tx[2] = 0xFFFFFFF0U;
            } else {
                mem_copy32(tx + 8U, (const uint32_t *)(uintptr_t)arg0, len >> 2);
                data_len = len;
            }
        } else if (op == OP_SET_MODE) {
            nand_set_mode(arg0 & 3U);
            tx[5] = REG32(NAND_CTL);
            tx[6] = REG32(NAND_REG_5C);
        } else if (op == OP_LOAD_CHUNK) {
            /* arg0 = byte offset in DMA_SCRATCH_ADDR (0, 256, 512, 768), arg1 = byte len (<=256, word multiple) */
            if ((arg0 & 3U) != 0U || (arg1 & 3U) != 0U || arg1 == 0U || arg1 > 256U || (arg0 + arg1) > 1024U || bc < (32U + arg1)) {
                tx[2] = 0xFFFFFFF0U;
            } else {
                uint32_t *dst = (uint32_t *)(DMA_SCRATCH_ADDR + arg0);
                uint32_t words = arg1 >> 2;
                mem_copy32(dst, &cmd[8], words);
                /* Compute 32-bit word sum of entire 1024B DMA_SCRATCH_ADDR for host verification */
                const uint32_t *all = (const uint32_t *)DMA_SCRATCH_ADDR;
                uint32_t sum = 0U;
                for (uint32_t i = 0U; i < 256U; i++) {
                    sum += all[i];
                }
                tx[4] = sum;
            }
        } else if (op == OP_ERASE_BLOCK) {
            /* arg0 = block, arg1 = UNLOCK_MAGIC */
            if (arg1 != UNLOCK_MAGIC) {
                tx[2] = 0xFFFFFFE0U;
            } else {
                nand_select_ce(0U);
                uint32_t st = 0xFFFFFFFFU;
                tx[2] = (uint32_t)nand_erase_block(arg0, &st);
                tx[4] = st;
            }
        } else if (op == OP_PROG_PAGE_START) {
            /* arg0 = row, arg1 = UNLOCK_MAGIC */
            if (arg1 != UNLOCK_MAGIC) {
                tx[2] = 0xFFFFFFE0U;
            } else {
                nand_select_ce(0U);
                tx[2] = (uint32_t)nand_prog_page_start(arg0);
            }
        } else if (op == OP_PROG_SECTOR) {
            /* arg0 = col_data | (col_oob << 16), arg1 = u0, arg2 = u1, arg3 = flags, arg4 = UNLOCK_MAGIC */
            if (arg4 != UNLOCK_MAGIC) {
                tx[2] = 0xFFFFFFE0U;
            } else {
                tx[2] = (uint32_t)nand_prog_sector(arg0, arg1, arg2, arg3);
            }
        } else if (op == OP_PROG_PAGE_FINISH) {
            /* arg0 = UNLOCK_MAGIC */
            if (arg0 != UNLOCK_MAGIC) {
                tx[2] = 0xFFFFFFE0U;
            } else {
                uint32_t st = 0xFFFFFFFFU;
                tx[2] = (uint32_t)nand_prog_page_finish(&st);
                tx[4] = st;
            }
        } else if (op == OP_NAND_RESET) {
            nand_select_ce(arg0 & 3U);
            tx[2] = (uint32_t)nand_reset();
            tx[4] = nand_read_status();
        } else if (op == OP_WDT_REBOOT) {
            tx[3] = 0U;
            (void)usb_send_stream((const uint8_t *)tx, 32U);
            delay_loops(50000U);
            REG32(0xB0010010U) = 0x140U;
            REG32(RTC_WDCTL) = 0x51U;
            for (;;) {
                __asm__ volatile ("nop");
            }
        } else {
            tx[2] = 0xFFFFFFFEU;
        }

        tx[3] = data_len;
        (void)usb_send_stream((const uint8_t *)tx, 32U + data_len);
    }
}

void main(void)
{
    wdt_kick();
    g_max_pkt = (REG32(BROM_HS_FLAG) != 0U) ? 512U : 64U;
    run_initial_probe();
    command_loop();
}
