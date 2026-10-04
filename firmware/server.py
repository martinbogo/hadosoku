"""HTTP + WebSocket server and the scope acquisition loop."""
import asyncio
import binascii
import gc
import hashlib
import json
import os
import struct
import time

from machine import Pin

import capture
import gen

VERSION = '1.0'
GUID = b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
MAGIC = 0x53


class WS:
    """Server side of one WebSocket connection (no fragmentation)."""

    def __init__(self, r, w):
        self.r, self.w = r, w
        self.lock = asyncio.Lock()
        self.open = True

    async def send(self, payload, binary=False, prefix=b''):
        n = len(prefix) + len(payload)
        op = 0x82 if binary else 0x81
        if n < 126:
            head = struct.pack('BB', op, n)
        else:
            head = struct.pack('>BBH', op, 126, n)
        async with self.lock:
            w = self.w
            w.write(head + prefix)
            mv = memoryview(payload)
            for off in range(0, len(mv), 1460):
                w.write(mv[off:off + 1460])
                await w.drain()
            if not len(mv):
                await w.drain()

    async def recv(self):
        """Next text message, or None once the peer closes."""
        while True:
            h = await self.r.readexactly(2)
            op, n = h[0] & 0x0F, h[1] & 0x7F
            if n == 126:
                n = struct.unpack('>H', await self.r.readexactly(2))[0]
            elif n == 127:
                n = struct.unpack('>Q', await self.r.readexactly(8))[0]
            mask = await self.r.readexactly(4) if h[1] & 0x80 else None
            data = bytearray(await self.r.readexactly(n)) if n else bytearray()
            if mask:
                for i in range(n):
                    data[i] ^= mask[i & 3]
            if op == 8:
                self.open = False
                return None
            if op == 9:
                async with self.lock:
                    self.w.write(struct.pack('BB', 0x8A, n) + data)
                    await self.w.drain()
            elif op in (1, 2):
                return bytes(data)


class Scope:
    def __init__(self, ip, mode, wlan):
        self.ip, self.mode, self.wlan = ip, mode, wlan
        self.cap = capture.Capture()
        self.cfg = {'mask': 1, 'rate': 100000, 'len': 1000, 'pre': 500,
                    'src': 0, 'level': 2048, 'hyst': 40, 'edge': 'r',
                    'mode': 'auto', 'run': True, 'roll': False}
        self.client = None
        self.gen = gen.Gen()
        self.hdr = bytearray(16)
        self.seq = 0
        self.t0 = time.ticks_ms()
        self.led = Pin('LED', Pin.OUT)

    def info(self):
        return {'t': 'hello', 'fw': VERSION, 'ip': self.ip, 'net': self.mode,
                'maxrate': capture.MAX_RATE, 'minrate': capture.MIN_RATE,
                'nbuf': capture.NBUF, 'cfg': self.cfg, 'gen': self.gen.snapshot(),
                'heap': gc.mem_free(), 'pins': ['GP26', 'GP27', 'GP28', 'GP29', 'TEMP']}

    def apply(self, m):
        t = m.get('t')
        if t == 'cfg':
            for k in self.cfg:
                if k in m:
                    self.cfg[k] = m[k]
        elif t == 'run':
            self.cfg['run'] = bool(m.get('on', True))
        elif t == 'gen':
            return {'t': 'genstate', 'gen': self.gen.apply(m)}
        elif t == 'fire':
            return {'t': 'genstate', 'gen': self.gen.fire()}

    async def stat(self, ws, state, frames, t_stat, **extra):
        now = time.ticks_ms()
        dt = max(1, time.ticks_diff(now, t_stat))
        gc.collect()
        self.gen.poll()
        st = {'t': 'stat', 'state': state, 'fps': round(frames * 1000 / dt, 1),
              'heap': gc.mem_free(), 'up': time.ticks_diff(now, self.t0) // 1000}
        st.update(extra)
        if self.mode == 'station':
            try:
                st['rssi'] = self.wlan.status('rssi')
            except Exception:
                pass
        await ws.send(json.dumps(st))
        self.led.toggle()

    async def roll(self, ws):
        """Stream continuously (no trigger) until the config changes."""
        cap = self.cap
        c = self.cfg
        mask = (c['mask'] & 0x1F) or 1
        nch = bin(mask).count('1')
        rate = max(1000, min(int(c['rate']), capture.MAX_RATE // nch))
        key = (mask, rate)
        half = max(256, min(4096, rate // 4, capture.NBUF // nch // 2))
        cap.start_ring(mask, rate * nch, half)
        chunks, t_stat = 0, time.ticks_ms()
        await self.stat(ws, 'roll', 0, t_stat)
        try:
            while True:
                c = self.cfg
                if not (c['run'] and c['roll'] and ((c['mask'] & 0x1F) or 1, int(c['rate'])) == key):
                    break
                r = cap.ring_take()
                if r:
                    off, nb = r
                    self.seq = (self.seq + 1) & 0xFFFF
                    struct.pack_into('<BBHBBHIHH', self.hdr, 0, MAGIC, 2, self.seq,
                                     mask, nch, nb // 2 // nch, int(cap.rate), 0xFFFF, 0)
                    await ws.send(memoryview(cap.buf)[off:off + nb], True, self.hdr)
                    chunks += 1
                if time.ticks_diff(time.ticks_ms(), t_stat) > 1000:
                    await self.stat(ws, 'roll', chunks, t_stat)
                    chunks, t_stat = 0, time.ticks_ms()
                await asyncio.sleep_ms(25)
        finally:
            cap.stop()

    async def acquire(self, ws):
        cap = self.cap
        frames = 0
        t_stat = time.ticks_ms()
        state = 'stop'
        while True:
            c = self.cfg
            if not c['run']:
                if state != 'stop':
                    state = 'stop'
                    await ws.send(json.dumps({'t': 'stat', 'state': state}))
                await asyncio.sleep_ms(30)
                continue
            if c['roll']:
                await self.roll(ws)
                state = 'roll'
                t_stat, frames = time.ticks_ms(), 0
                continue
            mask = (c['mask'] & 0x1F) or 1
            nch = bin(mask).count('1')
            trig_mode = c['mode']
            n = max(32, min(int(c['len']), capture.NBUF // nch // 2))
            pre = max(0, min(int(c['pre']), n - 1))
            total = n if trig_mode == 'free' else 2 * n
            rate = max(capture.MIN_RATE, min(int(c['rate']), capture.MAX_RATE // nch))
            cap.start(mask, rate * nch, total)
            await asyncio.sleep_ms(max(0, int(total / cap.rate * 1000) - 2))
            while cap.busy():
                await asyncio.sleep_ms(1)
            cap.finish()

            idx = -1
            if trig_mode != 'free':
                chans = cap.channels
                ci = chans.index(c['src']) if c['src'] in chans else 0
                idx = cap.find_trigger(ci, int(c['level']), int(c['hyst']),
                                       c['edge'] == 'r', pre, n - pre)
            if idx >= 0:
                first, flags, new = idx - pre, 1, 'trig'
            elif trig_mode in ('normal', 'single'):
                new, first = 'wait', -1
            else:
                first, flags, new = 0, 0, ('auto' if trig_mode == 'auto' else 'run')

            if first >= 0:
                self.seq = (self.seq + 1) & 0xFFFF
                struct.pack_into('<BBHBBHIHH', self.hdr, 0, MAGIC, flags,
                                 self.seq, mask, nch, n, int(cap.rate),
                                 pre if idx >= 0 else 0xFFFF, 0)
                await ws.send(cap.window(first, n), True, self.hdr)
                frames += 1
                if trig_mode == 'single':
                    c['run'] = False
                    new = 'stop'
                    await ws.send(json.dumps({'t': 'stat', 'state': 'stop', 'single': 1}))
            now = time.ticks_ms()
            if new != state or time.ticks_diff(now, t_stat) > 1000:
                dt = max(1, time.ticks_diff(now, t_stat))
                gc.collect()
                self.gen.poll()
                st = {'t': 'stat', 'state': new, 'fps': round(frames * 1000 / dt, 1),
                      'heap': gc.mem_free(),
                      'up': time.ticks_diff(now, self.t0) // 1000}
                if self.mode == 'station':
                    try:
                        st['rssi'] = self.wlan.status('rssi')
                    except Exception:
                        pass
                state = new
                if time.ticks_diff(now, t_stat) > 1000:
                    frames, t_stat = 0, now
                await ws.send(json.dumps(st))
                self.led.toggle()
            await asyncio.sleep_ms(0)

    async def session(self, ws):
        if self.client is not None:
            old = self.client
            old.open = False
            try:
                await old.send(json.dumps({'t': 'bye', 'why': 'taken'}))
                old.w.close()
            except Exception:
                pass
        self.client = ws
        await ws.send(json.dumps(self.info()))
        task = asyncio.create_task(self.acquire(ws))
        try:
            while True:
                msg = await ws.recv()
                if msg is None:
                    break
                try:
                    reply = self.apply(json.loads(msg))
                except ValueError:
                    continue
                if reply:
                    await ws.send(json.dumps(reply))
        finally:
            task.cancel()
            self.cap.stop()
            if self.client is ws:
                self.client = None


async def serve_file(w, path, ctype, gz=True):
    try:
        size = os.stat(path)[6]
    except OSError:
        w.write(b'HTTP/1.0 404 Not Found\r\nContent-Length: 0\r\n\r\n')
        await w.drain()
        return
    head = 'HTTP/1.0 200 OK\r\nContent-Type: %s\r\nContent-Length: %d\r\n' % (ctype, size)
    if gz:
        head += 'Content-Encoding: gzip\r\n'
    w.write(head + 'Cache-Control: no-cache\r\nConnection: close\r\n\r\n')
    with open(path, 'rb') as f:
        while True:
            chunk = f.read(1024)
            if not chunk:
                break
            w.write(chunk)
            await w.drain()


def make_handler(scope):
    async def handle(r, w):
        ws = None
        try:
            line = await r.readline()
            parts = line.split()
            if len(parts) < 2:
                return
            path = parts[1].decode().split('?')[0]
            key = upgrade = None
            while True:
                h = await r.readline()
                if h in (b'\r\n', b'\n', b''):
                    break
                name, _, val = h.partition(b':')
                name = name.strip().lower()
                if name == b'sec-websocket-key':
                    key = val.strip()
                elif name == b'upgrade':
                    upgrade = val.strip().lower()
            if path == '/ws' and key and upgrade == b'websocket':
                acc = binascii.b2a_base64(hashlib.sha1(key + GUID).digest()).strip()
                w.write(b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n'
                        b'Connection: Upgrade\r\nSec-WebSocket-Accept: ' + acc + b'\r\n\r\n')
                await w.drain()
                ws = WS(r, w)
                await scope.session(ws)
            elif path in ('/', '/index.html'):
                await serve_file(w, '/www/index.html.gz', 'text/html; charset=utf-8')
            elif path == '/favicon.ico':
                w.write(b'HTTP/1.0 204 No Content\r\n\r\n')
                await w.drain()
            else:
                w.write(b'HTTP/1.0 404 Not Found\r\nContent-Length: 0\r\n\r\n')
                await w.drain()
        except (OSError, EOFError, asyncio.CancelledError):
            pass
        finally:
            try:
                w.close()
                await w.wait_closed()
            except Exception:
                pass
            gc.collect()
    return handle


async def main(ip, mode, wlan):
    gc.collect()
    scope = Scope(ip, mode, wlan)
    scope.gen.apply({'on': False})
    await asyncio.start_server(make_handler(scope), '0.0.0.0', 80)
    print('Hadosoku %s ready: http://%s/  (%s)  free=%d' % (VERSION, ip, mode, gc.mem_free()))
    while True:
        await asyncio.sleep(3600)
