"""DMA-driven ADC capture with software edge trigger (RP2040).

The ADC free-runs at a programmable divider and a DMA channel streams its FIFO
into RAM. With several channels enabled the ADC round-robins them, so samples
are interleaved (ch0, ch1, ch0, ch1, ...) and each channel gets rate/nch.
"""
import micropython
import rp2
from machine import mem32

ADC = 0x4004C000
CS, FCS, FIFO, DIV = ADC + 0x00, ADC + 0x08, ADC + 0x0C, ADC + 0x10
DREQ_ADC = 36
DMA_BASE = 0x50000000
CLK = 48_000_000
MIN_CYCLES = 96            # one conversion; 500 kS/s ceiling
MAX_RATE = CLK // MIN_CYCLES
MIN_RATE = CLK // 65536 + 1
NBUF = 32768               # total uint16 samples across all channels
VREF = 3.3


def div_for(rate):
    d = round(CLK / rate) - 1
    # A divider at or just above the 96-cycle conversion time races it and
    # drops every other trigger, so near the ceiling free-run instead.
    return 0 if d < 100 else min(d, 0xFFFF)


def actual_rate(div):
    return CLK / MIN_CYCLES if div == 0 else CLK / (div + 1)


@micropython.viper
def find_rising(buf: ptr16, nch: int, ch: int, lo: int, hi: int, level: int,
                low: int) -> int:
    i = lo
    j = lo * nch + ch
    while i < hi:
        if int(buf[j]) < low:
            break
        i += 1
        j += nch
    while i < hi:
        if int(buf[j]) >= level:
            return i
        i += 1
        j += nch
    return -1


@micropython.viper
def find_falling(buf: ptr16, nch: int, ch: int, lo: int, hi: int, level: int,
                 high: int) -> int:
    i = lo
    j = lo * nch + ch
    while i < hi:
        if int(buf[j]) > high:
            break
        i += 1
        j += nch
    while i < hi:
        if int(buf[j]) <= level:
            return i
        i += 1
        j += nch
    return -1


class Capture:
    def __init__(self):
        self.buf = bytearray(2 * NBUF)
        self.dma = rp2.DMA()
        self.dma2 = rp2.DMA()      # partner channel for ring (roll) capture
        self.nch = 1
        self.channels = (0,)
        self.n = 0                 # per-channel samples requested
        self.rate = 0.0            # per-channel effective rate

    def stop(self):
        mem32[CS] &= ~(1 << 3)     # START_MANY off
        for d in (self.dma, self.dma2):
            if d.active():
                d.active(False)
        while not (mem32[FCS] & (1 << 8)):   # FIFO EMPTY flag
            mem32[FIFO]

    def _adc_setup(self, mask, total_rate):
        self.stop()
        chans = tuple(c for c in range(5) if mask >> c & 1)
        self.channels = chans
        self.nch = nch = len(chans)
        div = div_for(total_rate)
        self.rate = actual_rate(div) / nch
        mem32[FCS] = (1 << 0) | (1 << 3) | (1 << 24) | (3 << 10)  # EN, DREQ_EN, THRESH=1, clear ovf/unf
        mem32[DIV] = div << 8
        rr = mask if nch > 1 else 0
        mem32[CS] = 1 | (chans[0] << 12) | (rr << 16)
        return nch

    def start(self, mask, total_rate, per_ch_samples):
        """Begin a capture. mask: bit per ADC input 0..3 (GP26..29 / temp=4)."""
        nch = self._adc_setup(mask, total_rate)
        self.n = n = min(per_ch_samples, NBUF // nch)
        ctrl = self.dma.pack_ctrl(size=1, inc_read=False, inc_write=True,
                                  treq_sel=DREQ_ADC)
        self.dma.config(read=FIFO, write=self.buf, count=n * nch, ctrl=ctrl,
                        trigger=True)
        mem32[CS] |= 1 << 3        # START_MANY

    def busy(self):
        return self.dma.active()

    def finish(self):
        mem32[CS] &= ~(1 << 3)

    def find_trigger(self, ch_index, level, hyst, rising, pre, post):
        """Per-channel index of a trigger with `pre` samples before and `post`
        after available, or -1."""
        lo, hi = pre, self.n - post
        if hi <= lo:
            return -1
        if rising:
            return find_rising(self.buf, self.nch, ch_index, lo, hi, level,
                               level - hyst)
        return find_falling(self.buf, self.nch, ch_index, lo, hi, level,
                            level + hyst)

    def window(self, first, count):
        """Interleaved uint16 LE bytes for `count` per-channel samples."""
        k = self.nch * 2
        return memoryview(self.buf)[first * k:(first + count) * k]

    # ---- continuous (roll) capture: two chained DMA channels ping-pong ----

    def start_ring(self, mask, total_rate, per_ch_half):
        """Stream forever into two halves of `buf`; drain with ring_take()."""
        nch = self._adc_setup(mask, total_rate)
        self.half = per_ch_half * nch          # uint16 samples per half
        self.dmas = (self.dma, self.dma2)
        self.cur = 0
        self.sent = 0
        mv = memoryview(self.buf)
        hb = self.half * 2
        self.views = (mv[:hb], mv[hb:2 * hb])
        self.ctrl = (
            self.dma.pack_ctrl(size=1, inc_read=False, inc_write=True,
                               treq_sel=DREQ_ADC, chain_to=self.dma2.channel),
            self.dma2.pack_ctrl(size=1, inc_read=False, inc_write=True,
                                treq_sel=DREQ_ADC, chain_to=self.dma.channel))
        self._arm(0)
        self._arm(1)
        self.dma.active(True)
        mem32[CS] |= 1 << 3

    def _arm(self, i):
        self.dmas[i].config(read=FIFO, write=self.views[i], count=self.half,
                            ctrl=self.ctrl[i], trigger=False)

    def ring_take(self):
        """New data since the last call as (byte offset, byte count), or None.

        Always a whole number of interleaved frames. Overrun (the consumer
        falling a full half behind) sets self.overrun.
        """
        d = self.dmas[self.cur]
        remaining = mem32[DMA_BASE + 0x40 * d.channel + 8]
        complete = remaining == 0 and not d.active()
        k = self.half - remaining - self.sent
        if not complete:
            k -= k % self.nch
        off = self.cur * self.half * 2 + self.sent * 2
        if complete:
            self._arm(self.cur)
            self.cur ^= 1
            self.sent = 0
        else:
            self.sent += max(k, 0)
        return (off, k * 2) if k > 0 else None
