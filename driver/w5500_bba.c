/*
 * w5500_bba.c - WIZnet W5500 (SPI) network driver for dcplaya 2.0 Beta 2,
 *               presented to dcplaya as its "bba" network plugin.
 *
 * This file is built as a LEF (relocatable ELF, gzipped -> .LEZ) plugin and
 * replaces DCPLAYA/PLUGINS/EXE/NET/BBA/BBA.LEZ on the disc.  dcplaya's
 * NET.LEZ loads "plugins/exe/net/bba/bba.lez", looks up the symbols
 * _driver_init, _driver_shutdown and _netif, calls driver_init() and then
 * (*netif)->if_detect().
 *
 * driver_init():
 *   1. probe a W5500 on the SCI port (hardware sync-serial SPI, CS = PA7),
 *   2. else probe a W5500 on the SCIF port (bit-banged SPI, CS = RTS),
 *   3. else chain-load the ORIGINAL RTL8139 BBA driver, shipped on the disc
 *      as plugins/exe/net/bba/rtl8139.lez, and expose its netif instead.
 *   So a real Broadband Adapter keeps working exactly as before.
 *
 * IMPORTANT: this plugin is linked at runtime against the symbols exported
 * by the 2004 dcplaya executable (an old KallistiOS, ~1.2/1.3).  It must NOT
 * include modern KOS headers: every structure/prototype it uses is declared
 * below and was checked against the original binary (BBA.LEZ disassembly and
 * the decompiled net_dev_init / net_input).  The dcplaya LEF loader only
 * understands R_SH_DIR32 relocations.
 *
 * W5500 register logic ported from KallistiOS
 *   kernel/arch/dreamcast/hardware/network/w5500_adapter.c (C) 2025 Ruslan Rostovtsev
 *   kernel/arch/dreamcast/hardware/sci.c / scif-spi.c
 */

/* ------------------------------------------------------------------------- */
/* Old KOS ABI (as used by dcplaya 2.0b2)                                     */
/* ------------------------------------------------------------------------- */

#include "oldkos.h"

/* Symbols looked up by dcplaya's NET.LEZ */
netif_t *netif = NULL;
int driver_init(void);
int driver_shutdown(void);

/* ------------------------------------------------------------------------- */
/* SH4 on-chip peripherals                                                   */
/* ------------------------------------------------------------------------- */

#define REG8(a)  (*(volatile uint8  *)(a))
#define REG16(a) (*(volatile uint16 *)(a))
#define REG32(a) (*(volatile uint32 *)(a))

/* SCI (channel 1) */
#define SCSMR1  REG8(0xFFE00000)
#define SCBRR1  REG8(0xFFE00004)
#define SCSCR1  REG8(0xFFE00008)
#define SCTDR1  REG8(0xFFE0000C)
#define SCSSR1  REG8(0xFFE00010)
#define SCRDR1  REG8(0xFFE00014)
#define SCSPTR1 REG8(0xFFE00018)

/* SCIF (channel 2) */
#define SCSMR2  REG16(0xFFE80000)
#define SCSCR2  REG16(0xFFE80008)
#define SCFSR2  REG16(0xFFE80010)
#define SCFCR2  REG16(0xFFE80018)
#define SCSPTR2 REG16(0xFFE80020)
#define SCLSR2  REG16(0xFFE80024)

#define STBCR   REG8(0xFFC00004)
#define STBCR_SCI_STP 0x01

#define PCTRA   REG32(0xFF80002C)
#define PDTRA   REG16(0xFF800030)
#define CS_PA7_MASK   (3u << 14)
#define CS_PA7_OUT    (1u << 14)
#define CS_PA7_BIT    (1u << 7)

/* SCSSR1 bits */
#define TDRE 0x80
#define RDRF 0x40
#define ORER 0x20
#define FER  0x10
#define PER  0x08
#define TEND 0x04
/* SCSCR1 bits */
#define TE   0x20
#define RE   0x10
/* SCSMR1 */
#define SCSMR_CA 0x80

/* SCSPTR2 bits */
#define PTR2_RTSIO  0x80
#define PTR2_RTSDT  0x40
#define PTR2_CTSIO  0x20
#define PTR2_CTSDT  0x10
#define PTR2_SPB2IO 0x02
#define PTR2_SPB2DT 0x01

#define SPI_TIMEOUT 500000

/* ------------------------------------------------------------------------- */
/* Small helpers                                                              */
/* ------------------------------------------------------------------------- */

static inline uint8 bitrev8(uint8 b)
{
    b = (uint8)(((b & 0xF0) >> 4) | ((b & 0x0F) << 4));
    b = (uint8)(((b & 0xCC) >> 2) | ((b & 0x33) << 2));
    b = (uint8)(((b & 0xAA) >> 1) | ((b & 0x55) << 1));
    return b;
}

/* ~>=1.5us: each P4 register read costs several peripheral bus cycles. */
static void short_delay(void)
{
    int i;
    for (i = 0; i < 128; i++)
        (void)SCSSR1;
}

/* ------------------------------------------------------------------------- */
/* SPI transport: SCI (hardware synchronous serial, LSB first -> bit reverse) */
/* ------------------------------------------------------------------------- */

static uint8 sci_xfer_mode;
static int   spi_err;

static void sci_set_mode(uint8 mode)
{
    uint8 r = SCSCR1;
    if (mode == 0) {
        SCSCR1 = r & (uint8)~(TE | RE);
    } else if (sci_xfer_mode != mode) {
        if (sci_xfer_mode) {
            r &= (uint8)~(TE | RE);
            SCSCR1 = r;
            short_delay();
        }
        SCSCR1 = r | mode;
    }
    sci_xfer_mode = mode;
}

static int sci_wait(uint8 bit)
{
    int t = 0;
    while (!(SCSSR1 & bit)) {
        if (++t > SPI_TIMEOUT) {
            sci_set_mode(0);
            spi_err = 1;
            return -1;
        }
    }
    return 0;
}

static int sci_spi_init(void)
{
    int t;

    if (STBCR & STBCR_SCI_STP) {
        STBCR &= (uint8)~STBCR_SCI_STP;
        thd_sleep(1);
    }

    SCSCR1 = 0;            /* TE/RE off, internal clock, SCK = output */
    SCSPTR1 = 0;

    /* CS on PA7 GPIO, idle high */
    PCTRA = (PCTRA & ~CS_PA7_MASK) | CS_PA7_OUT;
    PDTRA |= CS_PA7_BIT;

    /* 8-bit synchronous mode, n = 0, BRR = 0 -> 50MHz / 4 = 12.5 MHz */
    SCSMR1 = SCSMR_CA;
    SCBRR1 = 0;
    thd_sleep(1);

    sci_xfer_mode = 0;
    SCSSR1 &= (uint8)~(ORER | FER | PER);
    if (SCSSR1 & RDRF)
        (void)SCRDR1;

    for (t = 0; !(SCSSR1 & TDRE); t++)
        if (t > SPI_TIMEOUT)
            return -1;
    return 0;
}

static void sci_spi_shutdown(void)
{
    SCSCR1 = 0;
    sci_xfer_mode = 0;
    PDTRA |= CS_PA7_BIT;
    PCTRA = PCTRA & ~CS_PA7_MASK;      /* PA7 back to input */
    STBCR |= STBCR_SCI_STP;
}

static void sci_cs(int active)
{
    uint16 v = PDTRA;
    if (active)
        v &= (uint16)~CS_PA7_BIT;
    else
        v |= CS_PA7_BIT;
    PDTRA = v;
}

static void sci_write(const uint8 *d, int len)
{
    sci_set_mode(TE);
    while (len-- > 0) {
        if (sci_wait(TDRE))
            return;
        SCTDR1 = bitrev8(*d++);
        SCSSR1 &= (uint8)~TDRE;
    }
    sci_wait(TEND);
}

static void sci_read(uint8 *d, int len)
{
    uint8 st;
    int t;

    sci_set_mode(TE | RE);
    if (sci_wait(TDRE))
        return;
    SCTDR1 = 0xFF;                      /* dummy byte clocked repeatedly */
    while (len-- > 0) {
        SCSSR1 &= (uint8)~TDRE;
        t = 0;
        do {
            st = SCSSR1;
            if (st & ORER) {
                SCSSR1 &= (uint8)~ORER;
                sci_set_mode(0);
                spi_err = 1;
                return;
            }
            if (++t > SPI_TIMEOUT) {
                sci_set_mode(0);
                spi_err = 1;
                return;
            }
        } while (!(st & RDRF));
        *d++ = bitrev8(SCRDR1);
        SCSSR1 &= (uint8)~RDRF;
    }
}

/* ------------------------------------------------------------------------- */
/* SPI transport: SCIF pins bit-banged (CLK=CTS, MOSI=TX, MISO=RX, CS=RTS)    */
/* ------------------------------------------------------------------------- */

static uint16 scsptr2;

#define SD_WAIT() __asm__ volatile("nop\n\tnop\n\tnop\n\tnop\n\tnop")

static int scif_spi_init(void)
{
    if (dcload_type == DCLOAD_TYPE_SER)
        return -1;                      /* serial port busy with dcload */
    SCSCR2 = 0;
    SCFCR2 = 0x06;
    SCFCR2 = 0;
    SCSMR2 = 0;
    SCFSR2 = 0;
    SCLSR2 = 0;
    SCSPTR2 = scsptr2 = PTR2_RTSIO | PTR2_RTSDT | PTR2_CTSIO | PTR2_SPB2IO;
    return 0;
}

static void scif_spi_shutdown(void)
{
    scif_init();                        /* give the port back to the kernel */
}

static void scif_cs(int active)
{
    if (active)
        scsptr2 &= (uint16)~PTR2_RTSDT;
    else
        scsptr2 |= PTR2_RTSDT;
    SCSPTR2 = scsptr2;
}

static void scif_write(const uint8 *d, int len)
{
    uint16 tmp = scsptr2 & (uint16)~(PTR2_CTSDT | PTR2_SPB2DT);
    while (len-- > 0) {
        uint8 b = *d++;
        int i;
        for (i = 7; i >= 0; i--) {
            uint16 bit = (b >> i) & 1;
            SCSPTR2 = tmp | bit;
            SCSPTR2 = tmp | bit | PTR2_CTSDT;
            SD_WAIT();
        }
    }
    SCSPTR2 = tmp;
}

static void scif_read(uint8 *d, int len)
{
    uint16 tmp = (scsptr2 & (uint16)~PTR2_CTSDT) | PTR2_SPB2DT;
    while (len-- > 0) {
        uint8 b = 0;
        int i;
        for (i = 0; i < 8; i++) {
            SCSPTR2 = tmp;
            SCSPTR2 = tmp | PTR2_CTSDT;
            b = (uint8)((b << 1) | (SCSPTR2 & PTR2_SPB2DT));
        }
        *d++ = b;
    }
    SCSPTR2 = tmp & (uint16)~PTR2_CTSDT;
}

/* ------------------------------------------------------------------------- */
/* Transport selection                                                        */
/* ------------------------------------------------------------------------- */

enum { BUS_NONE = 0, BUS_SCI = 1, BUS_SCIF = 2 };
static int bus = BUS_NONE;

static int spi_open(int which)
{
    bus = which;
    spi_err = 0;
    if (which == BUS_SCI)
        return sci_spi_init();
    return scif_spi_init();
}

static void spi_close(void)
{
    if (bus == BUS_SCI)
        sci_spi_shutdown();
    else if (bus == BUS_SCIF)
        scif_spi_shutdown();
    bus = BUS_NONE;
}

static inline void spi_cs(int a)
{
    if (bus == BUS_SCI) sci_cs(a); else scif_cs(a);
}

static inline void spi_write(const uint8 *d, int len)
{
    if (bus == BUS_SCI) sci_write(d, len); else scif_write(d, len);
}

static inline void spi_read(uint8 *d, int len)
{
    if (bus == BUS_SCI) sci_read(d, len); else scif_read(d, len);
}

/* ------------------------------------------------------------------------- */
/* W5500 access (variable data length mode)                                  */
/* ------------------------------------------------------------------------- */

#define BLK_COMMON 0x00
#define BLK_S0_REG 0x01
#define BLK_S0_TX  0x02
#define BLK_S0_RX  0x03

#define W_MR      0x0000
#define W_SHAR    0x0009
#define W_IMR     0x0016
#define W_SIMR    0x0018
#define W_PHYCFGR 0x002E
#define W_VERSION 0x0039

#define Sn_MR       0x0000
#define Sn_CR       0x0001
#define Sn_IR       0x0002
#define Sn_SR       0x0003
#define Sn_RXBUF    0x001E
#define Sn_TXBUF    0x001F
#define Sn_TX_FSR   0x0020
#define Sn_TX_WR    0x0024
#define Sn_RX_RSR   0x0026
#define Sn_RX_RD    0x0028

#define CR_OPEN  0x01
#define CR_CLOSE 0x10
#define CR_SEND  0x20
#define CR_RECV  0x40

#define MR_RST         0x80
#define Sn_MR_MACRAW   0x04
#define Sn_MR_MFEN     0x80
#define SOCK_MACRAW    0x42

static inline void w_hdr(uint8 *h, uint8 blk, uint16 addr, int write)
{
    h[0] = (uint8)(addr >> 8);
    h[1] = (uint8)addr;
    h[2] = (uint8)((blk << 3) | (write ? 0x04 : 0x00));
}

static void w_read_buf(uint8 blk, uint16 addr, uint8 *buf, int len)
{
    uint8 h[3];
    w_hdr(h, blk, addr, 0);
    spi_cs(1);
    spi_write(h, 3);
    spi_read(buf, len);
    spi_cs(0);
}

static void w_write_buf(uint8 blk, uint16 addr, const uint8 *buf, int len)
{
    uint8 h[3];
    w_hdr(h, blk, addr, 1);
    spi_cs(1);
    spi_write(h, 3);
    spi_write(buf, len);
    spi_cs(0);
}

static uint8 w_read8(uint8 blk, uint16 addr)
{
    uint8 v = 0;
    w_read_buf(blk, addr, &v, 1);
    return v;
}

static void w_write8(uint8 blk, uint16 addr, uint8 v)
{
    w_write_buf(blk, addr, &v, 1);
}

static uint16 w_read16(uint8 blk, uint16 addr)
{
    uint8 b[2] = { 0, 0 };
    w_read_buf(blk, addr, b, 2);
    return (uint16)((b[0] << 8) | b[1]);
}

static void w_write16(uint8 blk, uint16 addr, uint16 v)
{
    uint8 b[2];
    b[0] = (uint8)(v >> 8);
    b[1] = (uint8)v;
    w_write_buf(blk, addr, b, 2);
}

/* RSR/FSR may change while being read: read until two reads agree. */
static uint16 w_read16_stable(uint8 blk, uint16 addr)
{
    uint16 a, b;
    int n = 0;
    a = w_read16(blk, addr);
    do {
        b = a;
        a = w_read16(blk, addr);
    } while (a != b && ++n < 16);
    return a;
}

static int w_cmd(uint8 cmd, int may_sleep)
{
    int i;
    w_write8(BLK_S0_REG, Sn_CR, cmd);
    for (i = 0; i < 2000; i++) {
        if (w_read8(BLK_S0_REG, Sn_CR) == 0)
            return 0;
        if (may_sleep && (i & 63) == 63)
            thd_pass();
    }
    return -1;
}

static int w_soft_reset(void)
{
    int i;
    w_write8(BLK_COMMON, W_MR, MR_RST);
    for (i = 0; i < 20; i++) {
        thd_sleep(2);
        if (!(w_read8(BLK_COMMON, W_MR) & MR_RST))
            return 0;
    }
    return -1;
}

/* ------------------------------------------------------------------------- */
/* Driver state                                                               */
/* ------------------------------------------------------------------------- */

static netif_t w5500_if;
static semaphore_t *w_lock;
static kthread_t *rx_thd;
static volatile int rx_exit, rx_running;
static volatile int link_up;
static int registered;
static uint8 rx_buf[1536] __attribute__((aligned(32)));
static uint8 tx_pad[64] __attribute__((aligned(4)));

/* Original RTL8139 driver (fallback when no W5500 is found). */
static lef_prog_t *orig_lef;
static int (*orig_shutdown)(void);

static const char *bus_name(int b)
{
    return b == BUS_SCI ? "SCI" : (b == BUS_SCIF ? "SCIF" : "none");
}

static int lock_take(void)
{
    if (irq_inside_int())
        return sem_trywait(w_lock) ? -1 : 0;
    sem_wait(w_lock);
    return 0;
}

static void lock_give(void)
{
    sem_signal(w_lock);
}

static int w_probe(int which)
{
    uint8 ver;

    if (spi_open(which) < 0) {
        bus = BUS_NONE;
        return -1;
    }
    if (w_soft_reset() < 0 || spi_err) {
        spi_close();
        return -1;
    }
    ver = w_read8(BLK_COMMON, W_VERSION);
    if (ver != 0x04 || spi_err) {
        spi_close();
        return -1;
    }
    return 0;           /* leave the bus open */
}

static void w_make_mac(uint8 *mac)
{
    /* BIOS system ID (unique per console) -> locally administered MAC. */
    uint32 *vec = (uint32 *)0x8C0000B0;
    void (*sys_init)(int, int, int, int);
    uint8 *(*sys_id)(int, int, int, int);
    uint8 *id = NULL;
    int old;

    old = irq_disable();
    sys_init = (void (*)(int, int, int, int))(*vec);
    sys_init(0, 0, 0, 0);
    sys_id = (uint8 *(*)(int, int, int, int))(*vec);
    id = sys_id(0, 0, 0, 3);
    irq_restore(old);

    mac[0] = 0x02;
    if (id && ((id[3] | id[4] | id[5] | id[6] | id[7]) != 0) &&
        ((id[3] & id[4] & id[5] & id[6] & id[7]) != 0xFF)) {
        mac[1] = id[3]; mac[2] = id[4]; mac[3] = id[5];
        mac[4] = id[6]; mac[5] = id[7];
    } else {
        mac[1] = 0x09; mac[2] = 0xBF; mac[3] = 0x72;
        mac[4] = 0x24; mac[5] = 0x01;
    }
}

static int w_hw_init(netif_t *self)
{
    int i;

    if (w_soft_reset() < 0)
        return -1;

    /* PHY: reset, then auto-negotiation all capable, normal operation */
    w_write8(BLK_COMMON, W_PHYCFGR, 0x00);
    thd_sleep(2);
    w_write8(BLK_COMMON, W_PHYCFGR, 0xB8);

    if (w_read8(BLK_COMMON, W_VERSION) != 0x04) {
        printf("w5500: chip version mismatch\n");
        return -1;
    }

    w_write8(BLK_COMMON, W_IMR, 0x00);
    w_write8(BLK_COMMON, W_SIMR, 0x00);

    for (i = 0; i < 8; i++) {
        uint8 blk = (uint8)(1 + 4 * i);
        w_write8(blk, Sn_RXBUF, 0);
        w_write8(blk, Sn_TXBUF, 0);
    }
    w_write8(BLK_S0_REG, Sn_RXBUF, 16);
    w_write8(BLK_S0_REG, Sn_TXBUF, 16);

    w_write_buf(BLK_COMMON, W_SHAR, self->mac_addr, 6);

    w_write8(BLK_S0_REG, Sn_MR, Sn_MR_MACRAW | Sn_MR_MFEN);
    if (w_cmd(CR_OPEN, 1) < 0 || w_read8(BLK_S0_REG, Sn_SR) != SOCK_MACRAW) {
        printf("w5500: cannot open socket 0 in MACRAW mode\n");
        return -1;
    }
    return spi_err ? -1 : 0;
}

static int w_check_link(void)
{
    int up = w_read8(BLK_COMMON, W_PHYCFGR) & 1;
    if (up != link_up) {
        link_up = up;
        printf("w5500: link %s\n", up ? "up" : "down");
    }
    return up;
}

/* Returns 1 if a frame was handled, 0 if nothing pending. */
static int w_rx_one(netif_t *self)
{
    uint16 rsr, rd, len;
    uint8 hd[2];
    int n;

    if (lock_take() < 0)
        return 0;

    rsr = w_read16_stable(BLK_S0_REG, Sn_RX_RSR);
    if (rsr == 0) {
        w_check_link();
        lock_give();
        return 0;
    }

    rd = w_read16(BLK_S0_REG, Sn_RX_RD);
    w_read_buf(BLK_S0_RX, rd, hd, 2);
    len = (uint16)((hd[0] << 8) | hd[1]);

    if (len < 2 || len > rsr) {
        /* Corrupted state: drop everything currently buffered. */
        rd = (uint16)(rd + rsr);
        w_write16(BLK_S0_REG, Sn_RX_RD, rd);
        w_cmd(CR_RECV, 1);
        lock_give();
        return 1;
    }

    n = len - 2;
    if (n > (int)sizeof(rx_buf))
        n = sizeof(rx_buf);
    if (n > 0)
        w_read_buf(BLK_S0_RX, (uint16)(rd + 2), rx_buf, n);

    rd = (uint16)(rd + len);
    w_write16(BLK_S0_REG, Sn_RX_RD, rd);
    w_cmd(CR_RECV, 1);
    lock_give();

    if (n >= 14 && (self->flags & NETIF_RUNNING))
        net_input(self, rx_buf, n);
    return 1;
}

#define RX_BATCH 32     /* frames handled per wake-up */
#define RX_PRIO  9      /* KOS default is 10; lower runs first */

extern void *lef_find_symbol_all(const char *name);

static void w_rx_thread(void *param)
{
    netif_t *self = (netif_t *)param;
    int idle = 0;

    rx_running = 1;
    while (!rx_exit) {
        int n = 0;
        /* Drain everything the W5500 has buffered in one go.  Handing over
           one frame per thd_pass() made each frame wait behind a full time
           slice of the (CPU-bound) video decoder. */
        while (n < RX_BATCH && !rx_exit && w_rx_one(self))
            n++;
        if (n) {
            idle = 0;
            if (n >= RX_BATCH)
                thd_sleep(1);   /* flood guard: we run above default prio */
            else
                thd_pass();
        } else {
            /* No IRQ line from the W5500: poll.  Short sleeps keep latency
               low for streaming. */
            thd_sleep(idle < 50 ? 2 : 5);
            if (idle < 1000) idle++;
        }
    }
    rx_running = 0;
}

/* ------------------------------------------------------------------------- */
/* netif callbacks                                                            */
/* ------------------------------------------------------------------------- */

static int w_if_detect(netif_t *self)
{
    if (self->flags & NETIF_DETECTED)
        return 0;
    return -1;      /* detection is done in driver_init() */
}

static int w_if_init(netif_t *self)
{
    int r;
    if (self->flags & NETIF_INITIALIZED)
        return 0;
    sem_wait(w_lock);
    r = w_hw_init(self);
    sem_signal(w_lock);
    if (r < 0)
        return -1;
    self->flags |= NETIF_INITIALIZED;
    return 0;
}

static int w_if_start(netif_t *self)
{
    int i, up = 0;

    if (!(self->flags & NETIF_INITIALIZED))
        return -1;
    if (self->flags & NETIF_RUNNING)
        return 0;

    /* Wait up to ~6s for auto-negotiation. */
    for (i = 0; i < 120; i++) {
        sem_wait(w_lock);
        up = w_check_link();
        sem_signal(w_lock);
        if (up)
            break;
        thd_sleep(50);
    }
    if (!up)
        printf("w5500: no link yet (cable?), starting anyway\n");

    rx_exit = 0;
    rx_thd = thd_create(w_rx_thread, self);
    if (!rx_thd) {
        printf("w5500: cannot create RX thread\n");
        return -1;
    }
    thd_set_label(rx_thd, "W5500-rx-thd");
    {
        /* Run just above the default priority (10) so received frames reach
           lwIP while the decoder is busy.  Looked up at runtime: a missing
           symbol must not stop the driver from loading. */
        int (*set_prio)(kthread_t *, int) =
            (int (*)(kthread_t *, int))lef_find_symbol_all("_thd_set_prio");
        if (set_prio)
            set_prio(rx_thd, RX_PRIO);
    }

    self->flags |= NETIF_RUNNING;
    return 0;
}

static int w_if_stop(netif_t *self)
{
    int i;
    if (!(self->flags & NETIF_RUNNING))
        return 0;
    self->flags &= ~NETIF_RUNNING;
    rx_exit = 1;
    for (i = 0; i < 200 && rx_running; i++)
        thd_sleep(10);
    rx_thd = NULL;
    return 0;
}

static int w_if_shutdown(netif_t *self)
{
    w_if_stop(self);
    if (bus != BUS_NONE) {
        sem_wait(w_lock);
        w_write8(BLK_COMMON, W_MR, MR_RST);
        spi_close();
        sem_signal(w_lock);
    }
    self->flags &= ~(NETIF_INITIALIZED | NETIF_DETECTED);
    return 0;
}

static int w_if_tx(netif_t *self, const uint8 *data, int len, int blocking)
{
    uint16 fsr, wr;
    int tries = 0;

    if (!(self->flags & NETIF_RUNNING) || len <= 0 || len > 1514)
        return NETIF_TX_ERROR;

    if (lock_take() < 0)
        return NETIF_TX_ERROR;

    for (;;) {
        fsr = w_read16_stable(BLK_S0_REG, Sn_TX_FSR);
        if (fsr >= (uint16)(len < 60 ? 60 : len))
            break;
        if (!blocking || irq_inside_int() || ++tries > 200) {
            lock_give();
            return blocking ? NETIF_TX_ERROR : NETIF_TX_AGAIN;
        }
        lock_give();
        thd_pass();
        lock_take();
    }

    wr = w_read16(BLK_S0_REG, Sn_TX_WR);
    if (len < 60) {
        /* pad runt frames to the Ethernet minimum */
        memset(tx_pad, 0, 60);
        memcpy(tx_pad, data, len);
        data = tx_pad;
        len = 60;
    }
    w_write_buf(BLK_S0_TX, wr, data, len);
    w_write16(BLK_S0_REG, Sn_TX_WR, (uint16)(wr + len));
    if (w_cmd(CR_SEND, !irq_inside_int()) < 0) {
        lock_give();
        return NETIF_TX_ERROR;
    }
    lock_give();
    return NETIF_TX_OK;
}

static int w_if_tx_commit(netif_t *self)
{
    (void)self;
    return 0;
}

static int w_if_rx_poll(netif_t *self)
{
    (void)self;
    return 0;       /* frames are delivered by the RX thread */
}

static int w_if_set_flags(netif_t *self, uint32 flags_and, uint32 flags_or)
{
    uint32 old = self->flags;
    self->flags = (self->flags & flags_and) | flags_or;

    if (((old ^ self->flags) & NETIF_PROMISC) && (self->flags & NETIF_INITIALIZED)) {
        uint8 mode = Sn_MR_MACRAW | ((self->flags & NETIF_PROMISC) ? 0 : Sn_MR_MFEN);
        sem_wait(w_lock);
        w_cmd(CR_CLOSE, 1);
        w_write8(BLK_S0_REG, Sn_MR, mode);
        w_cmd(CR_OPEN, 1);
        sem_signal(w_lock);
    }
    return 0;
}

/* ------------------------------------------------------------------------- */
/* Fallback: original Broadband Adapter driver                                */
/* ------------------------------------------------------------------------- */

static int load_original_bba(void)
{
    char path[256];
    int (*o_init)(void);
    netif_t **o_netif;

    shell_home_path(path, sizeof(path), "plugins/exe/net/bba/rtl8139.lez");
    orig_lef = lef_load(path);
    if (!orig_lef) {
        printf("bba(w5500): cannot load original driver '%s'\n", path);
        return -1;
    }
    o_init = (int (*)(void))lef_find_symbol(orig_lef, "_driver_init");
    orig_shutdown = (int (*)(void))lef_find_symbol(orig_lef, "_driver_shutdown");
    o_netif = (netif_t **)lef_find_symbol(orig_lef, "_netif");
    if (!o_init || !orig_shutdown || !o_netif) {
        printf("bba(w5500): bad original driver\n");
        lef_free(orig_lef);
        orig_lef = NULL;
        orig_shutdown = NULL;
        return -1;
    }
    if (o_init()) {
        /* its own init failed; let it clean up */
        orig_shutdown();
        lef_free(orig_lef);
        orig_lef = NULL;
        orig_shutdown = NULL;
        return -1;
    }
    netif = *o_netif;
    return 0;
}

/* ------------------------------------------------------------------------- */
/* DHCP hooks (same for W5500 and for the original BBA driver)               */
/* ------------------------------------------------------------------------- */

static int (*under_start)(netif_t *self);
static int (*under_tx)(netif_t *self, const uint8 *data, int len, int blocking);

static int dhcp_wrap_start(netif_t *self)
{
    int r = under_start(self);
    if (r == 0 && dhcp_requested())
        dhcp_run(self, under_tx);
    return r;
}

static int dhcp_wrap_tx(netif_t *self, const uint8 *data, int len, int blocking)
{
    dhcp_fixup();
    return under_tx(self, data, len, blocking);
}

static void install_dhcp_hooks(netif_t *n)
{
    dhcp_reset();
    under_start = n->if_start;
    under_tx = n->if_tx;
    n->if_start = dhcp_wrap_start;
    n->if_tx = dhcp_wrap_tx;
}

/* ------------------------------------------------------------------------- */
/* Plugin entry points                                                        */
/* ------------------------------------------------------------------------- */

int lef_main(void)
{
    return 0;
}

int driver_init(void)
{
    netif = NULL;
    registered = 0;

    if (!w_lock)
        w_lock = sem_create(1);
    if (!w_lock)
        return -1;

    memset(&w5500_if, 0, sizeof(w5500_if));

    if (w_probe(BUS_SCI) == 0 || w_probe(BUS_SCIF) == 0) {
        w5500_if.name = "bba";
        w5500_if.descr = "WIZnet W5500 Ethernet (SPI) as BBA";
        w5500_if.flags = NETIF_DETECTED;
        w_make_mac(w5500_if.mac_addr);
        w5500_if.if_detect = w_if_detect;
        w5500_if.if_init = w_if_init;
        w5500_if.if_shutdown = w_if_shutdown;
        w5500_if.if_start = w_if_start;
        w5500_if.if_stop = w_if_stop;
        w5500_if.if_tx = w_if_tx;
        w5500_if.if_tx_commit = w_if_tx_commit;
        w5500_if.if_rx_poll = w_if_rx_poll;
        w5500_if.if_set_flags = w_if_set_flags;

        printf("bba(w5500): W5500 found on %s, MAC %02x:%02x:%02x:%02x:%02x:%02x\n",
               bus_name(bus),
               w5500_if.mac_addr[0], w5500_if.mac_addr[1], w5500_if.mac_addr[2],
               w5500_if.mac_addr[3], w5500_if.mac_addr[4], w5500_if.mac_addr[5]);

        netif = &w5500_if;
        install_dhcp_hooks(&w5500_if);
        if (net_reg_device(&w5500_if) == 0)
            registered = 1;
        return 0;
    }

    printf("bba(w5500): no W5500 found, trying original Broadband Adapter\n");
    if (load_original_bba() == 0) {
        install_dhcp_hooks(netif);
        return 0;
    }

    /* Nothing usable: expose an inert netif so that NET.LEZ's
       if_detect() call fails cleanly and it tries the next driver. */
    w5500_if.name = "bba";
    w5500_if.descr = "no adapter";
    w5500_if.if_detect = w_if_detect;
    w5500_if.if_init = w_if_init;
    w5500_if.if_shutdown = w_if_shutdown;
    w5500_if.if_start = w_if_start;
    w5500_if.if_stop = w_if_stop;
    w5500_if.if_tx = w_if_tx;
    w5500_if.if_tx_commit = w_if_tx_commit;
    w5500_if.if_rx_poll = w_if_rx_poll;
    w5500_if.if_set_flags = w_if_set_flags;
    netif = &w5500_if;
    return 0;
}

int driver_shutdown(void)
{
    if (orig_lef) {
        if (orig_shutdown)
            orig_shutdown();
        lef_free(orig_lef);
        orig_lef = NULL;
        orig_shutdown = NULL;
    } else {
        if (w5500_if.flags & (NETIF_INITIALIZED | NETIF_RUNNING | NETIF_DETECTED) ||
            bus != BUS_NONE)
            w_if_shutdown(&w5500_if);
        if (registered) {
            net_unreg_device(&w5500_if);
            registered = 0;
        }
    }
    dhcp_reset();
    netif = NULL;
    if (w_lock) {
        sem_destroy(w_lock);
        w_lock = NULL;
    }
    return 0;
}
