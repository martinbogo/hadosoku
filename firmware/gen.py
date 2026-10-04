"""Signal generator on GP15.

Digital waves (square, pulse) come from a PIO state machine with exact cycle
counts. Analog waves (sine, triangle, ramps, noise, dc) are PWM-encoded: a DMA
channel streams a waveform table into the PWM duty register, so the average
voltage follows the shape and the pin chops at the carrier frequency. A smooth
analog signal needs an external RC low-pass filter.
"""
import math
import os
import rp2
import struct
from machine import Pin, PWM, mem32, freq as sys_freq
from uctypes import addressof

PIN = 15
TABLE = 1024                                # PWM duty entries per DMA loop
CARRIER = 309000.0     # PWM carrier: ~0.618 x 500 kS/s, so the ADC samples it at well-spread phases
FT_MAX = 1000000.0     # fastest table update rate
FT_MIN = 2000.0        # DMA timer limit (16-bit ratio); slower waves are paced by PWM wraps
DMA_TIMER0 = 0x50000420
TREQ_TIMER0 = 0x3B
VCC = 3.3
SLICE = 7                                   # GP15 is PWM slice 7, channel B
PWM_BASE = 0x40050000 + 0x14 * SLICE
CC_ADDR = PWM_BASE + 0x0C
DREQ_PWM = 24 + SLICE
GPIO_CTRL = 0x40014000 + 8 * PIN + 4

DIGITAL = ('square', 'pulse')
ANALOG = ('sine', 'triangle', 'ramp_up', 'ramp_down', 'noise', 'dc')
WAVES = DIGITAL + ANALOG


@rp2.asm_pio(set_init=rp2.PIO.OUT_LOW)
def _train():
    # high = X_h + 3 cycles, low = X_l + 4 cycles, repeating forever
    pull(block)
    mov(y, osr)
    pull(block)
    label("loop")
    set(pins, 1)
    mov(x, y)
    label("hi")
    jmp(x_dec, "hi")
    set(pins, 0)
    mov(x, osr)
    label("lo")
    jmp(x_dec, "lo")
    jmp("loop")


@rp2.asm_pio(set_init=rp2.PIO.OUT_LOW)
def _burst():
    # high = X_h + 3 cycles, low = X_l + 7 cycles, then waits for a new command
    label("start")
    pull(block)
    mov(y, osr)
    pull(block)
    mov(isr, osr)
    pull(block)
    label("loop")
    set(pins, 1)
    mov(x, y)
    label("hi")
    jmp(x_dec, "hi")
    set(pins, 0)
    mov(x, osr)
    label("lo")
    jmp(x_dec, "lo")
    mov(x, isr)
    jmp(x_dec, "more")
    jmp("start")
    label("more")
    mov(isr, x)
    jmp("loop")


def _clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


def _ratio(r, lim=65535):
    """Best X/Y approximation of r with X, Y <= lim (continued fractions)."""
    h0, h1, k0, k1 = 0, 1, 1, 0
    x = r
    for _ in range(40):
        a = int(x)
        h2, k2 = a * h1 + h0, a * k1 + k0
        if h2 > lim or k2 > lim:
            break
        h0, h1, k0, k1 = h1, h2, k1, k2
        frac = x - a
        if frac < 1e-12:
            break
        x = 1.0 / frac
    return max(1, h1), max(1, k1)


class Gen:
    def __init__(self):
        self.s = {'on': False, 'wave': 'square', 'freq': 1000.0, 'duty': 50.0,
                  'amp': VCC, 'off': VCC / 2, 'sym': 50.0, 'width': 100e-6,
                  'count': 0, 'invert': False}
        self.info = {}
        self.sm = None
        self.pwm = None
        self.dma = None
        self._timer = None
        self._dch = None
        self.raw = None

    # ---- public ----

    def snapshot(self):
        d = dict(self.s)
        d.update(self.info)
        return d

    def apply(self, m):
        s = self.s
        for k in s:
            if k in m:
                s[k] = m[k]
        if s['wave'] not in WAVES:
            s['wave'] = 'square'
        s['freq'] = float(s['freq'])
        s['duty'] = _clamp(float(s['duty']), 0.0, 100.0)
        s['sym'] = _clamp(float(s['sym']), 1.0, 99.0)
        s['amp'] = _clamp(float(s['amp']), 0.0, VCC)
        s['off'] = _clamp(float(s['off']), 0.0, VCC)
        s['width'] = max(0.0, float(s['width']))
        s['count'] = max(0, int(s['count']))
        s['on'] = bool(s['on'])
        s['invert'] = bool(s['invert'])
        self._stop()
        self.info = {}
        if s['on']:
            if s['wave'] in DIGITAL:
                self._digital()
            else:
                self._analog()
        return self.snapshot()

    def fire(self):
        """Emit one burst in pulse mode with a finite count, restarting any
        burst already running. (Queueing into the 4-entry PIO FIFO would block
        the whole event loop on the second press.)"""
        s = self.s
        if s['on'] and s['wave'] == 'pulse' and s['count'] > 0:
            self._stop()
            self.info = {}
            self._digital()
        return self.snapshot()

    def poll(self):
        """Call about once a second: keeps the analog DMA stream running."""
        if self.dma is not None and not self.dma.active():
            self._dma_start()

    # ---- internals ----

    def _stop(self):
        if self.dma is not None:
            self.dma.active(False)
        if self.sm is not None:
            self.sm.active(0)
            self.sm = None
        if self.pwm is not None:
            self.pwm.deinit()
            self.pwm = None
        self.dma = None
        mem32[GPIO_CTRL] = mem32[GPIO_CTRL] & ~(3 << 8)   # normal output
        Pin(PIN, Pin.OUT, value=0)

    def _invert(self, on):
        mem32[GPIO_CTRL] = (mem32[GPIO_CTRL] & ~(3 << 8)) | ((1 if on else 0) << 8)

    def _digital(self):
        s = self.s
        sysf = sys_freq()
        burst = s['wave'] == 'pulse' and s['count'] > 0
        min_l = 7 if burst else 4
        f = _clamp(s['freq'], 0.02, sysf / 10)
        period = max(10, int(sysf / f + 0.5))
        if s['wave'] == 'square':
            high = int(period * s['duty'] / 100 + 0.5)
        else:
            high = int(s['width'] * sysf + 0.5)
        high = _clamp(high, 3, period - min_l)
        low = period - high
        prog = _burst if burst else _train
        self.sm = rp2.StateMachine(0, prog, freq=sysf, set_base=Pin(PIN))
        self._invert(s['invert'])
        self.sm.active(1)
        if burst:
            self._cmd = (high - 3, s['count'] - 1, low - 7)
            self._push_burst()
        else:
            self.sm.put(high - 3)
            self.sm.put(low - 4)
        self.info = {'digital': True, 'freq_actual': sysf / period,
                     'width_actual': high / sysf, 'duty_actual': high * 100.0 / period,
                     'period': period / sysf}

    def _push_burst(self):
        # Never call sm.put on a full FIFO: it blocks until space appears.
        for v in self._cmd:
            if self.sm.tx_fifo() >= 4:
                break
            self.sm.put(v & 0xFFFFFFFF)

    def _shape(self, p):
        s = self.s
        w = s['wave']
        if w == 'sine':
            return math.sin(2 * math.pi * p)
        if w == 'triangle':
            k = s['sym'] / 100
            return -1 + 2 * p / k if p < k else 1 - 2 * (p - k) / (1 - k)
        if w == 'ramp_up':
            return -1 + 2 * p
        return 1 - 2 * p        # ramp_down

    def _timing(self, carrier):
        """PWM divider (8.4 fixed point) and TOP for a carrier frequency."""
        total = sys_freq() / carrier                    # clock cycles per PWM period
        div16 = _clamp(int(total * 16 / 65536) + 1, 16, 4095)
        top = _clamp(int(total * 16 / div16 + 0.5) - 1, 3, 65535)
        return div16, top

    def _timing_exact(self, cycles):
        """(div16, top) so that div * (top + 1) is as close to `cycles` as possible."""
        lo = max(16, int(cycles * 16 / 65536) + 1)
        best = None
        for d16 in range(lo, min(lo + 64, 4096)):
            top1 = int(cycles * 16 / d16 + 0.5)
            if top1 > 65536 or top1 < 4:
                continue
            err = abs(d16 / 16 * top1 - cycles) / cycles
            if best is None or err < best[0]:
                best = (err, d16, top1 - 1)
            if err < 2e-4:
                break
        if best is None:
            return self._timing(sys_freq() / cycles * 1.0)
        return best[1], best[2]

    def _pwm_start(self, div16, top, cc):
        self.pwm = PWM(Pin(PIN))
        mem32[PWM_BASE] = 0
        mem32[PWM_BASE + 0x04] = div16
        mem32[PWM_BASE + 0x10] = top
        mem32[CC_ADDR] = cc << 16
        mem32[PWM_BASE] = 1

    def _cc(self, volts, top):
        return int(_clamp(volts / VCC, 0.0, 1.0) * (top + 1) + 0.5)

    def _analog(self):
        s = self.s
        w = s['wave']
        sysf = sys_freq()
        amp = s['amp']
        half = amp / 2
        off = _clamp(s['off'], half, VCC - half)
        if w == 'dc':
            div16, top = self._timing(122000)
            self._pwm_start(div16, top, self._cc(s['off'], top))
            self.info = {'digital': False, 'levels': top + 1, 'carrier': sysf * 16 / div16 / (top + 1),
                         'n': 0, 'freq_actual': 0.0, 'level_actual': self._cc(s['off'], top) / (top + 1) * VCC}
            return
        n = TABLE
        nbytes = n * 4
        timer = None
        if w == 'noise':
            periods = 1
            div16, top = self._timing(_clamp(s['freq'], 10.0, 500000.0))
        else:
            f = _clamp(s['freq'], 0.05, 100000.0)
            ft = f * n
            if ft >= FT_MIN:
                # table rate set by the DMA timer, carrier fixed independently
                periods = max(1, int(ft / FT_MAX))
                ft = f * n / periods
                timer = _ratio(ft / sysf)
                div16, top = self._timing_exact(sysf / CARRIER)
            else:
                periods = 1
                div16, top = self._timing_exact(sysf / ft)
        if self.raw is None:
            self.raw = bytearray(8192)
        off_b = (-addressof(self.raw)) % nbytes
        tbl = memoryview(self.raw)[off_b:off_b + nbytes]
        if w == 'noise':
            rnd = os.urandom(2 * n)
            for i in range(n):
                r = (rnd[2 * i] | rnd[2 * i + 1] << 8) / 65535 - 0.5
                struct.pack_into('<I', tbl, 4 * i, self._cc(off + amp * r, top) << 16)
        else:
            for i in range(n):
                v = off + half * self._shape((i * periods / n) % 1.0)
                struct.pack_into('<I', tbl, 4 * i, self._cc(v, top) << 16)
        self._tbl = tbl
        self._ring = 12
        self._pwm_start(div16, top, self._cc(off, top))
        carrier = sysf * 16 / div16 / (top + 1)
        self._timer = timer
        if w == 'noise':
            fa = carrier
        elif timer:
            fa = sysf * timer[0] / timer[1] * periods / n
        else:
            fa = carrier * periods / n
        self.info = {'digital': False, 'n': n // periods, 'levels': top + 1, 'carrier': carrier,
                     'freq_actual': fa}
        if self._dch is None:
            self._dch = rp2.DMA()
        self.dma = self._dch
        self._dma_start()

    def _dma_start(self):
        d = self.dma
        d.active(False)
        treq = DREQ_PWM
        if self._timer:
            mem32[DMA_TIMER0] = self._timer[0] << 16 | self._timer[1]
            treq = TREQ_TIMER0
        ctrl = d.pack_ctrl(size=2, inc_read=True, inc_write=False, treq_sel=treq,
                           ring_size=self._ring, ring_sel=0)
        d.config(read=self._tbl, write=CC_ADDR, count=0xFFFFFFF0, ctrl=ctrl, trigger=True)
