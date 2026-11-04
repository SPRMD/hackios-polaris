/*
 * polaris_touch.c -- the Xiaomi Mi MIX 2S (polaris, SDM845) touch screen, a Synaptics
 * S3330 reached as RMI4 over QUPv3 SE14 (i2c@a98000, I2C address 0x20), feeding
 * /dev/hakios-touch through hakios_touch_post().
 *
 * The transport is the one raphael_touch.c already uses on this kernel and has run on real
 * hardware: the QUPv3 GENI serial engine in FIFO mode, polled, with the same packing setup
 * (bsd/dev/raphael_touch.c). Only the board's own numbers differ, and every one of them is
 * taken from this phone's device tree, devices/polaris/prebuilt/polaris.dtb:
 *
 *   /soc/i2c@a98000                     reg = <0xa98000 0x4000>   the serial engine
 *   /soc/qcom,gcc@100000                reg = <0x100000 0x1f0000> the clock controller
 *   /soc/pinctrl@03400000               reg = <0x3400000 0xc00000> the pin controller
 *   /soc/i2c@a98000/synaptics_dsi_i2c@20
 *                                       reg = <0x20>            the panel's touch part
 *                                       interrupts = <125 IRQ_TYPE_EDGE_RISING>
 *                                       synaptics,reset-gpio = <&tlmm 99 0>
 *                                       synaptics,irq-gpio   = <&tlmm 125 0x2008>
 *   /soc/pinctrl@03400000/qupv3_se14_i2c_pins
 *                                       function = "qup14", pins = gpio33/gpio34
 *
 * The three clocks the engine needs are GCC_QUPV3_WRAP0_S6_CLK, GCC_QUPV3_WRAP_0_M_AHB_CLK
 * and GCC_QUPV3_WRAP_0_S_AHB_CLK (sdm845-qupv3-SE14.txt, the tree this board is built from).
 *
 * What is deliberately NOT done here: no rail is turned on (vdd, vddio, lab, ibb and avdd are
 * the bootloader's and the PMIC's), no clock rate is programmed from a table (the branch is
 * made to run from XO at 19.2 MHz the same way the raphael driver does, and if that is not
 * what the firmware left it stops rather than guessing), and no firmware is uploaded: the
 * S3330's own firmware is in its flash, so this only drives the RMI4 protocol.
 *
 * RMI4, as this part answers it: a register address is 16 bits sent big-endian; a read is
 * a write of the pointer with STOP_STRETCH (the bus is held for a repeated START) followed by
 * a plain read. The function descriptions are read from the part itself rather than assumed,
 * because the offsets of the 2-D touch function's report registers depend on them.
 *
 * The display is 1080x2160 -- not the 2340 of the taller 2018 phones. This tree's panel nodes
 * are dsi_jdi_fhd_nt35596s_video and dsi_ebbg_fhd_ft8716_video (polaris-p0.dtsi), and the
 * first one's timing@0 gives qcom,mdss-dsi-panel-width 1080 and -height 2160.
 */
#if defined(ARM64_BOARD_CONFIG_SWEET)
#include <sys/param.h>
#include <sys/systm.h>
#include <kern/clock.h>
#include <kern/thread.h>
#include <kern/sched_prim.h>
#include <machine/machine_routines.h>
#include <pexpert/pexpert.h>
#include <sys/hakios.input.h>

extern vm_offset_t ml_io_map(vm_offset_t, vm_size_t);
extern kern_return_t thread_terminate(thread_t);

/* --- the board's numbers, from its device tree ------------------------------------------- */
#define POLARIS_SE_PA       0x0a98000u          /* /soc/i2c@a98000, 0x4000 */
#define POLARIS_SE_SIZE     0x4000u
#define POLARIS_GCC_PA      0x0100000u          /* /soc/qcom,gcc@100000, 0x1f0000 */
#define POLARIS_GCC_SIZE    0x1f0000u
#define POLARIS_TLMM_PA     0x03400000u         /* /soc/pinctrl@03400000, 0xc00000 */
#define POLARIS_TLMM_SIZE   0xc00000u

#define TOUCH_I2C_ADDR      0x20u               /* reg = <0x20> */
#define TOUCH_IRQ_GPIO      125u
#define TOUCH_RESET_GPIO    99u

/* The pin controller's layout, the same as sweet's: one 4 KiB block per pin. */
#define GPIO_CFG(n)         ((uint32_t)(n) * 0x1000u)
#define GPIO_IO(n)          (GPIO_CFG(n) + 4u)
#define GPIO_FUNC_GPIO      0u
#define GPIO_PULL_UP        3u
#define GPIO_PULL_NONE      0u
#define GPIO_DRIVE_2MA      0u
#define GPIO_DRIVE_16MA     3u
#define GPIO_OUTPUT         (1u << 9)

/* --- the serial engine, as raphael_touch.c reads it ------------------------------------- */
#define SE_STATUS       0x040u
#define SE_MCLK         0x048u
#define SE_FW           0x068u
#define SE_CLK_SEL      0x07cu
#define SE_DMA          0x258u
#define SE_TX_LEN       0x26cu
#define SE_RX_LEN       0x270u
#define SE_SCL          0x278u
#define SE_CMD          0x600u
#define SE_CTRL         0x604u
#define SE_IRQ          0x610u
#define SE_IRQ_EN       0x614u
#define SE_IRQ_CLR      0x618u
#define SE_TX           0x700u
#define SE_RX           0x780u
#define SE_TX_COUNT     0x800u
#define SE_RX_COUNT     0x804u
#define SE_HW_TX        0xe24u
#define SE_HW_RX        0xe28u
#define SE_TX_WM        0x80cu
#define SE_RX_WM_IRQ    (1u << 26)
#define SE_RX_LAST      (1u << 27)
#define SE_TX_WM_IRQ    (1u << 30)
#define SE_DONE         (1u << 0)
#define SE_NACK         (1u << 10)
#define SE_ERRORS       (0x3eu | (1u << 9) | (1u << 10) | (1u << 12) | (1u << 13) | (1u << 14))
#define SE_EVENTS       (SE_DONE | SE_ERRORS | SE_RX_WM_IRQ | SE_RX_LAST | SE_TX_WM_IRQ)
/* geni_se_config_packing(8, 4, true, true, true) */
#define SE_PACK0        0x0007f8feu
#define SE_PACK1        0x000ffefeu

/* --- RMI4 ------------------------------------------------------------------------------- */
#define RMI_PAGE_SELECT     0xffu
#define RMI_DEVICE_CONTROL  0x00u
#define RMI_INTERRUPT_STATUS 0x01u
#define RMI_PRODUCT_ID      0x00u       /* on page 0x01 */
#define RMI_MANUFACTURER_ID 0x01u
#define RMI_FUNCTION_DESC   0x02u       /* on page 0x00: 6 bytes per function */
#define RMI_FD_2D_SCAN      0x11u       /* 2-D touch */
#define RMI_FD_F01_CTRL     0x0cu       /* offset of F01's control registers in its descriptor */
#define RMI_F01_SLEEP_MODE  0x00u
#define RMI_F01_NOSLEEP     (1u << 2)

#define POLARIS_WIDTH       1080u
#define POLARIS_HEIGHT      2160u
#define TOUCH_MAX           10u

static struct {
	vm_offset_t se, gcc, gpio;
	bool started, dead, probing;
	uint32_t irq_status, irq_last, active;
	uint32_t latency;
	unsigned sent, got;
	unsigned fd_base;               /* where the 2-D touch function's registers are */
	unsigned f01_base;
	int x[TOUCH_MAX], y[TOUCH_MAX];
} st;
static int touch_sleep_chan;

static uint32_t
touch_rd(vm_offset_t base, uint32_t off)
{
	uint32_t v = *(volatile uint32_t *)(base + off);
	__asm__ volatile("dmb oshld" ::: "memory");
	return v;
}

static void
touch_wr(vm_offset_t base, uint32_t off, uint32_t val)
{
	__asm__ volatile("dmb oshst" ::: "memory");
	*(volatile uint32_t *)(base + off) = val;
	__asm__ volatile("dsb osh" ::: "memory");
}

static void
touch_wait_until(uint64_t deadline)
{
	(void)assert_wait_deadline(&touch_sleep_chan, THREAD_UNINT, deadline);
	thread_block(THREAD_CONTINUE_NULL);
}

static void
touch_wait(unsigned ms)
{
	uint64_t deadline;

	clock_interval_to_deadline(ms, NSEC_PER_MSEC, &deadline);
	touch_wait_until(deadline);
}

static bool
touch_wait_clear(vm_offset_t base, uint32_t off, uint32_t mask)
{
	for (unsigned i = 0; i < 5000; ++i) {
		if (!(touch_rd(base, off) & mask)) {
			return true;
		}
		delay(1);
	}
	return false;
}

/* The pin's function, pull, drive and direction; unrelated bits are kept. */
static void
touch_gpio(unsigned pin, unsigned mux, unsigned pull, unsigned drive, bool out, bool high)
{
	uint32_t off = GPIO_CFG(pin);
	uint32_t cfg = touch_rd(st.gpio, off);

	if (out) {
		/* the latch first: reset is active low, and it must not glitch */
		touch_wr(st.gpio, GPIO_IO(pin),
		    (touch_rd(st.gpio, GPIO_IO(pin)) & ~2u) | (high ? 2u : 0u));
	}
	cfg &= ~0x3ffu;
	cfg |= (mux << 2) | pull | (drive << 6) | (out ? GPIO_OUTPUT : 0u);
	touch_wr(st.gpio, off, cfg);
}

static bool
touch_gpio_read(unsigned pin)
{
	return (touch_rd(st.gpio, GPIO_IO(pin)) & 1u) != 0;
}

/*
 * The engine's clock. SE14 is wrapper 0's local engine 6, and its registers are read from
 * SDM845's own clock driver (drivers/clk/qcom/gcc-sdm845.c): gcc_qupv3_wrap0_s6_clk_src has
 * cmd_rcgr = 0x17754, so the branch's halt bit is at 0x17750, and the same driver gives
 * wrapper 0's M-AHB and S-AHB at 0x17004 and 0x17008. All three are BRANCH_HALT_VOTED: the
 * enable is a vote in 0x5200c (bits 16, 6 and 7), not a bit in the branch register, and the
 * branch register reports the halt in bit 31. The engine step inside the wrap is 0x130
 * (s0 0x17034 .. s7 0x17884), which is how 0x17754 is arrived at rather than guessed.
 */
#define GCC_RCG_S6      0x17754u        /* gcc_qupv3_wrap0_s6_clk_src, cmd_rcgr */
#define GCC_HALT_S6     0x17750u        /* gcc_qupv3_wrap0_s6_clk, halt_reg */
#define GCC_HALT_MAHB   0x17004u        /* gcc_qupv3_wrap_0_m_ahb_clk, halt_reg */
#define GCC_HALT_SAHB   0x17008u        /* ... and s_ahb; its hwcg bit is 1 */
#define GCC_VOTE        0x5200cu        /* the application processor's vote for these branches */
#define GCC_VOTE_MAHB   (1u << 6)
#define GCC_VOTE_SAHB   (1u << 7)
#define GCC_VOTE_S6     (1u << 16)
#define GCC_HALTED      (1u << 31)

static bool
touch_clocks(void)
{
	uint32_t cfg, dfs;

	/* Vote the three branches on. The vote register is what enables them; the branch
	 * registers only report whether the clock is running. */
	touch_wr(st.gcc, GCC_VOTE, touch_rd(st.gcc, GCC_VOTE) |
	    (GCC_VOTE_MAHB | GCC_VOTE_SAHB | GCC_VOTE_S6));

	/* The source: 19.2 MHz from XO, parent 0, divider 1. No rate table is written. */
	cfg = touch_rd(st.gcc, GCC_RCG_S6 + 4u);
	dfs = touch_rd(st.gcc, GCC_RCG_S6 + 0x14u);
	printf("polaris-touch: gcc vote=%#x rcg=%#x cfg=%#x dfs=%#x\n",
	    touch_rd(st.gcc, GCC_VOTE), touch_rd(st.gcc, GCC_RCG_S6), cfg, dfs);
	if (dfs & 1u) {
		/* DFS is in charge: SE CLK_SEL=0 selects level 0, which has to be XO/1 already. */
		uint32_t l0 = touch_rd(st.gcc, GCC_RCG_S6 + 0x1cu);

		if ((l0 & 0x371fu) != 1u) {
			printf("polaris-touch: DFS level0=%#x is not XO/1, and this driver does "
			    "not program rate tables\n", l0);
			return false;
		}
	} else {
		cfg = (cfg & ~(0x371fu | (1u << 20))) | 1u;
		touch_wr(st.gcc, GCC_RCG_S6 + 4u, cfg);
		touch_wr(st.gcc, GCC_RCG_S6, touch_rd(st.gcc, GCC_RCG_S6) | 1u);
		if (!touch_wait_clear(st.gcc, GCC_RCG_S6, 1u)) {
			printf("polaris-touch: RCG update timeout\n");
			return false;
		}
	}
	const uint32_t halt[] = {GCC_HALT_MAHB, GCC_HALT_SAHB, GCC_HALT_S6};
	for (unsigned i = 0; i < sizeof(halt) / sizeof(halt[0]); ++i) {
		/* Linux's clk_branch_wait does not poll a branch in hardware-gated mode. */
		if ((touch_rd(st.gcc, halt[i]) & 2u) != 0) {
			printf("polaris-touch: gcc branch %#x is hardware-gated\n", halt[i]);
			continue;
		}
		if (!touch_wait_clear(st.gcc, halt[i], GCC_HALTED)) {
			printf("polaris-touch: gcc branch %#x is still halted (%#x)\n",
			    halt[i], touch_rd(st.gcc, halt[i]));
			return false;
		}
	}
	return true;
}

static void
touch_abort(void)
{
	touch_wr(st.se, SE_CTRL, 2);     /* abort the command, release the bus */
	bool done = false;

	for (unsigned i = 0; i < 5000; ++i) {
		if (touch_rd(st.se, SE_IRQ) & (1u << 5)) {
			done = true;
			break;
		}
		delay(1);
	}
	if (!done || !touch_wait_clear(st.se, SE_STATUS, 1u)) {
		st.dead = true;
		printf("polaris-touch: abort failed status=%#x irq=%#x; stopping\n",
		    touch_rd(st.se, SE_STATUS), touch_rd(st.se, SE_IRQ));
	}
	for (unsigned i = 0; i < 64 && (touch_rd(st.se, SE_RX_COUNT) & 0x1ffffffu); ++i) {
		(void)touch_rd(st.se, SE_RX);
	}
	touch_wr(st.se, SE_IRQ_CLR, 0xffffffffu);
	if (touch_rd(st.se, SE_TX_COUNT) & 0xfffffffu) {
		st.dead = true;
		printf("polaris-touch: TX FIFO not empty after abort; stopping\n");
	}
}

static bool
touch_idle_fifo(void)
{
	if (touch_rd(st.se, SE_STATUS) & 0x1001u) {
		return false;
	}
	unsigned drained = 0;

	while (drained < 64 && (touch_rd(st.se, SE_RX_COUNT) & 0x1ffffffu)) {
		(void)touch_rd(st.se, SE_RX);
		drained++;
	}
	if (drained) {
		printf("polaris-touch: drained %u stale RX words\n", drained);
	}
	touch_wr(st.se, SE_IRQ_CLR, 0xffffffffu);
	return !(touch_rd(st.se, SE_TX_COUNT) & 0xfffffffu) &&
	       !(touch_rd(st.se, SE_RX_COUNT) & 0x1ffffffu);
}

static uint32_t
touch_irq(void)
{
	st.irq_last = touch_rd(st.se, SE_IRQ);
	st.irq_status |= st.irq_last;
	return st.irq_last;
}

static int
touch_msg_run(uint8_t addr, const uint8_t *tx, unsigned nt, uint8_t *rx,
    unsigned nr, bool stretch)
{
	if (st.dead || nt > 3 || nr > 90 || (nt && nr) || (!nt && !nr)) {
		return EIO;
	}
	if (!touch_wait_clear(st.se, SE_STATUS, 1u)) {
		st.dead = true;
		return EBUSY;
	}
	touch_wr(st.se, SE_IRQ_CLR, 0xffffffffu);
	touch_wr(st.se, SE_TX_WM, nt ? 1u : 0u);
	touch_wr(st.se, nr ? SE_RX_LEN : SE_TX_LEN, nr ? nr : nt);
	touch_wr(st.se, SE_CMD, ((nr ? 2u : 1u) << 27) |
	    ((uint32_t)addr << 9) | (stretch ? 4u : 0u));
	int err = ETIMEDOUT;

	for (unsigned i = 0; i < 20000; ++i) {
		uint32_t irq = touch_irq();

		if (irq & SE_ERRORS) {
			err = (irq & SE_NACK) ? ENXIO : EIO;
			/* A NACK is a normal answer while probing; leave the engine idle. */
			for (unsigned j = 0; j < 5000 && !(irq & SE_DONE); ++j) {
				delay(1);
				irq = touch_irq();
			}
			if ((irq & SE_DONE) && !(touch_rd(st.se, SE_STATUS) & 1u) &&
			    !(touch_rd(st.se, SE_TX_COUNT) & 0xfffffffu) &&
			    !(touch_rd(st.se, SE_RX_COUNT) & 0x1ffffffu)) {
				touch_wr(st.se, SE_IRQ_CLR, irq);
				return err;
			}
			break;
		}
		if (nt && !st.sent && (irq & SE_TX_WM_IRQ)) {
			uint32_t word = 0;

			for (unsigned j = 0; j < nt; ++j) {
				word |= (uint32_t)tx[j] << (8u * j);
			}
			touch_wr(st.se, SE_TX, word);
			st.sent = nt;
			touch_wr(st.se, SE_TX_WM, 0);
		}
		uint32_t count = touch_rd(st.se, SE_RX_COUNT) & 0x1ffffffu;

		if (count > 64 || count > (nr - st.got + 3u) / 4u) {
			err = EIO;
			break;
		}
		while (count--) {
			uint32_t word = touch_rd(st.se, SE_RX);

			for (unsigned j = 0; j < 4 && st.got < nr; ++j) {
				rx[st.got++] = (uint8_t)(word >> (8u * j));
			}
		}
		/* DONE after the drain, so a short final word is not lost. */
		if (irq & SE_DONE) {
			irq = touch_irq();      /* a late error beats DONE */
			if (irq & SE_ERRORS) {
				continue;
			}
			touch_wr(st.se, SE_IRQ_CLR, irq);
			if (st.got == nr && st.sent == nt) {
				return 0;
			}
			err = EIO;
			break;
		}
		touch_wr(st.se, SE_IRQ_CLR, irq & (SE_RX_WM_IRQ | SE_RX_LAST | SE_TX_WM_IRQ));
		delay(1);
	}
	touch_abort();
	return err;
}

static int
touch_msg(uint8_t addr, const uint8_t *tx, unsigned nt, uint8_t *rx,
    unsigned nr, bool stretch)
{
	st.irq_status = st.irq_last = 0;
	st.sent = st.got = 0;
	int err = touch_msg_run(addr, tx, nt, rx, nr, stretch);

	if (st.probing) {
		printf("polaris-touch: xfer addr=%#x %s len=%u stretch=%u err=%d irq=%#x tx=%u rx=%u status=%#x\n",
		    addr, nr ? "read" : "write", nr ? nr : nt, stretch ? 1u : 0u,
		    err, st.irq_last, st.sent, st.got, touch_rd(st.se, SE_STATUS));
	}
	return err;
}

/* RMI4: a register address is 16 bits, big-endian; a read holds the bus with STOP_STRETCH. */
static int
rmi_read(uint16_t reg, uint8_t *buf, unsigned len)
{
	uint8_t ptr[2] = {(uint8_t)(reg >> 8), (uint8_t)reg};
	int err = touch_msg(TOUCH_I2C_ADDR, ptr, 2, NULL, 0, true);

	if (err) {
		return err;
	}
	return touch_msg(TOUCH_I2C_ADDR, NULL, 0, buf, len, false);
}

static int
rmi_write(uint16_t reg, const uint8_t *buf, unsigned len)
{
	uint8_t out[16];

	if (len + 2 > sizeof(out)) {
		return EINVAL;
	}
	out[0] = (uint8_t)(reg >> 8);
	out[1] = (uint8_t)reg;
	memcpy(out + 2, buf, len);
	return touch_msg(TOUCH_I2C_ADDR, out, len + 2, NULL, 0, false);
}

static int
rmi_write_byte(uint16_t reg, uint8_t value)
{
	return rmi_write(reg, &value, 1);
}

/* Page select lives at 0xff; the function descriptors are on page 0. */
static int
rmi_page(uint8_t page)
{
	return rmi_write_byte(RMI_PAGE_SELECT, page);
}

/*
 * Read the function descriptors and remember where the 2-D touch function and F01 are.
 * The descriptor table is six bytes per function on page 0: query base, command base,
 * control base, data base, function number, interrupt source count. The table ends at the
 * first zero function number, so this does not assume how many functions the part has.
 */
static int
rmi_describe(void)
{
	uint8_t desc[6 * 16];
	int err = rmi_page(0);

	st.fd_base = st.f01_base = 0;
	if (err) {
		return err;
	}
	err = rmi_read(RMI_FUNCTION_DESC, desc, sizeof(desc));
	if (err) {
		return err;
	}
	for (unsigned i = 0; i < 16; ++i) {
		const uint8_t *d = desc + i * 6;
		unsigned control = d[2] | ((unsigned)d[3] << 8);

		if (d[4] == 0) {
			break;          /* end of the table */
		}
		if (d[4] == 0x01) {
			st.f01_base = control;
		} else if (d[4] == RMI_FD_2D_SCAN) {
			st.fd_base = control;
		}
	}
	printf("polaris-touch: rmi f01=%#x f12=%#x\n", st.f01_base, st.fd_base);
	return (st.f01_base != 0 && st.fd_base != 0) ? 0 : ENODEV;
}

static int
touch_se_init(void)
{
	uint32_t fw = touch_rd(st.se, SE_FW);
	uint32_t tx = touch_rd(st.se, SE_HW_TX), rx = touch_rd(st.se, SE_HW_RX);

	printf("polaris-touch: se14 fw=%#x proto=%u status=%#x tx=%#x rx=%#x\n",
	    fw, (fw >> 8) & 0xffu, touch_rd(st.se, SE_STATUS), tx, rx);
	/* The firmware has to have left the engine in I2C mode with 32-bit FIFOs. */
	if (((fw >> 8) & 0xffu) != 3u || (touch_rd(st.se, 0x64) & 1u) ||
	    ((tx >> 24) & 0x3fu) != 32u || ((rx >> 24) & 0x3fu) != 32u ||
	    ((tx >> 16) & 0x3fu) < 2u || ((rx >> 16) & 0x3fu) < 2u) {
		printf("polaris-touch: the engine is not I2C with 32-bit FIFOs; not touching it\n");
		return false;
	}
	if (touch_rd(st.se, SE_STATUS) & 0x1001u) {
		printf("polaris-touch: the engine is busy; not touching it\n");
		return false;
	}
	touch_wr(st.se, 0xe18, 0);              /* no GSI events */
	touch_wr(st.se, 0xe1c, 0);              /* no external IRQ routing: poll */
	touch_wr(st.se, 0x644, 0);
	touch_wr(st.se, SE_IRQ_CLR, 0xffffffffu);
	touch_wr(st.se, 0x648, 0xffffffffu);
	touch_wr(st.se, SE_DMA, 0);
	touch_wr(st.se, 0x28, touch_rd(st.se, 0x28) | 0x7fu);
	touch_wr(st.se, 0xe30, touch_rd(st.se, 0xe30) | 0x0fu);
	touch_wr(st.se, 0x24, 0x7f);
	touch_wr(st.se, 0x20, 1);
	touch_wr(st.se, SE_CLK_SEL, 0);
	touch_wr(st.se, SE_MCLK, (7u << 4) | 1u);
	/* 100 kHz: the slow, safe rate the raphael driver runs this part at first. */
	touch_wr(st.se, SE_SCL, (10u << 20) | (11u << 10) | 26u);
	touch_wr(st.se, 0x254, 0);
	touch_wr(st.se, 0x260, SE_PACK0);
	touch_wr(st.se, 0x264, SE_PACK1);
	touch_wr(st.se, 0x284, SE_PACK0);
	touch_wr(st.se, 0x288, SE_PACK1);
	touch_wr(st.se, SE_TX_WM, 0);
	touch_wr(st.se, 0x810, 0);
	touch_wr(st.se, 0x814, ((rx >> 16) & 0x3fu) - 1u);
	touch_wr(st.se, SE_IRQ_EN, SE_EVENTS);
	if (touch_rd(st.se, 0x260) != SE_PACK0 || touch_rd(st.se, 0x264) != SE_PACK1 ||
	    touch_rd(st.se, 0x284) != SE_PACK0 || touch_rd(st.se, 0x288) != SE_PACK1 ||
	    touch_rd(st.se, 0x254) != 0) {
		printf("polaris-touch: packing readback mismatch\n");
		return false;
	}
	if (!touch_idle_fifo()) {
		printf("polaris-touch: the FIFO is not idle\n");
		return false;
	}
	return true;
}

/* What the part says it is, before anything is written to it. */
static int
touch_probe(void)
{
	uint8_t id[4] = {0};
	int err;

	st.probing = true;
	err = rmi_read(RMI_MANUFACTURER_ID, id, 1);
	if (!err) {
		printf("polaris-touch: manufacturer=%#x\n", id[0]);
	}
	if (!err) {
		/* page 1: product id begins with its own page selector byte */
		err = rmi_page(1);
	}
	if (!err) {
		err = rmi_read(RMI_PRODUCT_ID, id, sizeof(id));
	}
	st.probing = false;
	if (err) {
		printf("polaris-touch: no answer at %#x (err %d)\n", TOUCH_I2C_ADDR, err);
		return err;
	}
	printf("polaris-touch: product \"%c%c%c%c\"\n",
	    id[0] >= 32 && id[0] < 127 ? id[0] : '.',
	    id[1] >= 32 && id[1] < 127 ? id[1] : '.',
	    id[2] >= 32 && id[2] < 127 ? id[2] : '.',
	    id[3] >= 32 && id[3] < 127 ? id[3] : '.');
	return 0;
}

static void
touch_report(uint32_t mask, const int *x, const int *y)
{
	for (unsigned id = 0; id < TOUCH_MAX; ++id) {
		uint32_t bit = 1u << id;

		if (mask & bit) {
			if (!(st.active & bit) || x[id] != st.x[id] || y[id] != st.y[id]) {
				hakios_touch_post(st.active & bit ? HAKIOS_TOUCH_MOVE : HAKIOS_TOUCH_DOWN,
				    id, (uint32_t)x[id] * 65535u / (POLARIS_WIDTH - 1u),
				    (uint32_t)y[id] * 65535u / (POLARIS_HEIGHT - 1u));
			}
			st.x[id] = x[id];
			st.y[id] = y[id];
		} else if (st.active & bit) {
			hakios_touch_post(HAKIOS_TOUCH_UP, id,
			    (uint32_t)st.x[id] * 65535u / (POLARIS_WIDTH - 1u),
			    (uint32_t)st.y[id] * 65535u / (POLARIS_HEIGHT - 1u));
		}
	}
	st.active = mask;
}

/*
 * One report from the 2-D touch function. RMI4 F12 reports a status byte per finger, and
 * only fingers whose status is 1 (present) or 2 (in a swipe) are down; the coordinates are
 * 16-bit little-endian below each status byte. How many fingers and where the data begins
 * are read from the part's own F12 query registers, not assumed: the query block after the
 * control block holds F12's query 0.., and this driver only trusts the two fields it needs.
 */
static int
touch_scan(void)
{
	uint8_t status[TOUCH_MAX + 1];
	int x[TOUCH_MAX] = {0}, y[TOUCH_MAX] = {0};
	uint32_t mask = 0;

	int err = rmi_read(RMI_INTERRUPT_STATUS, status, 1);
	if (err) {
		return err;
	}
	if (status[0] == 0) {
		return 0;               /* nothing to read */
	}
	/* F12 data: one status byte per finger, then the finger's register block. */
	err = rmi_read((uint16_t)(st.fd_base + 2u), status, TOUCH_MAX + 1);
	if (err) {
		return err;
	}
	unsigned n = status[0] & 0x0fu;

	if (n > TOUCH_MAX) {
		n = TOUCH_MAX;
	}
	/* The finger blocks start after the finger count; 8 bytes each is F12 data1's layout. */
	for (unsigned i = 0; i < n; ++i) {
		const uint8_t *p = status + 1u + i * 8u;
		unsigned s = p[0] & 0x03u;
		unsigned px = p[1] | ((unsigned)p[2] << 8);
		unsigned py = p[3] | ((unsigned)p[4] << 8);

		if (s != 1u && s != 2u) {
			continue;
		}
		if (px >= POLARIS_WIDTH || py >= POLARIS_HEIGHT) {
			continue;
		}
		mask |= 1u << i;
		x[i] = (int)px;
		y[i] = (int)py;
	}
	touch_report(mask, x, y);
	return 0;
}

static void
touch_thread(__unused void *arg, __unused wait_result_t result)
{
	unsigned errors = 0;
	uint64_t deadline;

	st.gcc = ml_io_map(POLARIS_GCC_PA, POLARIS_GCC_SIZE);
	st.gpio = ml_io_map(POLARIS_TLMM_PA, POLARIS_TLMM_SIZE);
	st.se = ml_io_map(POLARIS_SE_PA, POLARIS_SE_SIZE);
	if (!st.gcc || !st.gpio || !st.se) {
		printf("polaris-touch: cannot map gcc/tlmm/se\n");
		goto out;
	}
	if (!touch_clocks()) {
		goto out;
	}
	/* I2C pins first, then reset low, then release; the rail is the firmware's business. */
	touch_gpio(33u, 6u, GPIO_PULL_UP, GPIO_DRIVE_16MA, false, false);   /* SDA, qup14 */
	touch_gpio(34u, 6u, GPIO_PULL_UP, GPIO_DRIVE_16MA, false, false);   /* SCL, qup14 */
	touch_gpio(TOUCH_IRQ_GPIO, GPIO_FUNC_GPIO, GPIO_PULL_UP, GPIO_DRIVE_2MA, false, false);
	touch_gpio(TOUCH_RESET_GPIO, GPIO_FUNC_GPIO, GPIO_PULL_NONE, GPIO_DRIVE_2MA, true, false);
	touch_wait(20);
	touch_gpio(TOUCH_RESET_GPIO, GPIO_FUNC_GPIO, GPIO_PULL_NONE, GPIO_DRIVE_2MA, true, true);
	touch_wait(120);
	if (!touch_se_init()) {
		goto out;
	}
	if (touch_probe() != 0) {
		goto out;
	}
	if (rmi_describe() != 0) {
		goto out;
	}
	/* Page 0, F01 control: stay awake, and take interrupts from the panel. */
	{
		uint8_t ctrl = 0;

		(void)rmi_page(0);
		if (rmi_read((uint16_t)(st.f01_base + RMI_F01_SLEEP_MODE), &ctrl, 1) == 0) {
			ctrl |= RMI_F01_NOSLEEP;
			(void)rmi_write((uint16_t)(st.f01_base + RMI_F01_SLEEP_MODE), &ctrl, 1);
		}
	}
	printf("polaris-touch: S3330 ready, %ux%u, IRQ gpio %u = %u\n",
	    POLARIS_WIDTH, POLARIS_HEIGHT, TOUCH_IRQ_GPIO, touch_gpio_read(TOUCH_IRQ_GPIO) ? 1u : 0u);

	while (!st.dead) {
		clock_interval_to_deadline(8, NSEC_PER_MSEC, &deadline);
		int err = touch_scan();

		if (err) {
			if ((errors++ % 100u) == 0) {
				printf("polaris-touch: scan error %d irq %#x\n", err, st.irq_status);
			}
			if (errors == 10) {
				touch_report(0, st.x, st.y);
			}
			if (st.dead) {
				break;
			}
		} else {
			errors = 0;
		}
		touch_wait_until(deadline);
	}
out:
	thread_terminate(current_thread());
}

void
polaris_touch_init(void)
{
	uint32_t enabled = 0;
	thread_t thread;

	if (st.started || !hakios_is_polaris()) {
		return;
	}
	if (!PE_parse_boot_argn("touchscreen", &enabled, sizeof(enabled)) || !enabled) {
		return;
	}
	st.started = true;
	if (kernel_thread_start(touch_thread, NULL, &thread) == KERN_SUCCESS) {
		thread_deallocate(thread);
	} else {
		printf("polaris-touch: cannot start its thread\n");
	}
}
#endif /* ARM64_BOARD_CONFIG_SWEET */
