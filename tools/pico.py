#!/usr/bin/env python3
"""Minimal MicroPython host tool over the raw REPL (needs only pyserial).

  pico.py ls [dir]
  pico.py put LOCAL REMOTE
  pico.py rm REMOTE
  pico.py exec FILE.py [seconds]     run a script, print its output
  pico.py reset [seconds]            soft reset (runs main.py), print output
  pico.py deploy                     copy firmware/*.py, www gz, secrets.py
"""
import binascii
import glob
import gzip
import os
import sys
import time

import serial

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def find_port():
    ports = glob.glob('/dev/cu.usbmodem*')
    if not ports:
        sys.exit('No /dev/cu.usbmodem* port found')
    return ports[0]


class Pico:
    def __init__(self):
        self.s = serial.Serial(find_port(), 115200, timeout=0.2)

    def read_until(self, token, timeout):
        buf = b''
        end = time.time() + timeout
        while time.time() < end:
            buf += self.s.read(self.s.in_waiting or 1)
            if buf.endswith(token) or token in buf:
                return buf
        return buf

    def interrupt(self):
        self.s.write(b'\x03\x03')
        time.sleep(0.2)
        self.s.read(self.s.in_waiting)

    def enter_raw(self):
        self.interrupt()
        self.s.write(b'\x01')
        out = self.read_until(b'raw REPL; CTRL-B to exit\r\n>', 3)
        if b'raw REPL' not in out:
            sys.exit('Could not enter raw REPL: %r' % out)

    def exit_raw(self):
        self.s.write(b'\x02')

    def exec_raw(self, code, timeout=10):
        self.s.write(code.encode() if isinstance(code, str) else code)
        self.s.write(b'\x04')
        if self.read_until(b'OK', 3)[-2:] != b'OK':
            sys.exit('exec not acknowledged')
        out = b''
        end = time.time() + timeout
        while time.time() < end:
            out += self.s.read(self.s.in_waiting or 1)
            if out.count(b'\x04') >= 2:
                break
        data, _, rest = out.partition(b'\x04')
        err = rest.partition(b'\x04')[0]
        return data.decode(errors='replace'), err.decode(errors='replace')

    def put(self, local, remote, data=None):
        if data is None:
            data = open(local, 'rb').read()
        parts = remote.strip('/').split('/')[:-1]
        path = ''
        for p in parts:
            path += '/' + p
            self.exec_raw("import os\ntry:\n os.mkdir('%s')\nexcept OSError:\n pass\n" % path)
        out, err = self.exec_raw("import binascii\nf=open('%s','wb')\n" % remote)
        if err:
            sys.exit(err)
        for i in range(0, len(data), 512):
            chunk = binascii.b2a_base64(data[i:i + 512], newline=False).decode()
            out, err = self.exec_raw("f.write(binascii.a2b_base64('%s'))\n" % chunk)
            if err:
                sys.exit(err)
        self.exec_raw("f.close()\n")
        print('put %-28s %6d bytes' % (remote, len(data)))


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    cmd = sys.argv[1]
    p = Pico()
    if cmd == 'reset':
        secs = float(sys.argv[2]) if len(sys.argv) > 2 else 5
        p.interrupt()
        p.s.write(b'\x02')              # leave raw REPL if a previous command left it there
        time.sleep(0.2)
        p.s.write(b'\x04')
        end = time.time() + secs
        while time.time() < end:
            d = p.s.read(p.s.in_waiting or 1)
            if d:
                sys.stdout.write(d.decode(errors='replace'))
                sys.stdout.flush()
        return
    p.enter_raw()
    try:
        if cmd == 'ls':
            d = sys.argv[2] if len(sys.argv) > 2 else '/'
            out, err = p.exec_raw("import os\nprint(os.listdir('%s'))" % d)
            print(out, err)
        elif cmd == 'put':
            p.put(sys.argv[2], sys.argv[3])
        elif cmd == 'rm':
            print(p.exec_raw("import os\nos.remove('%s')" % sys.argv[2]))
        elif cmd == 'exec':
            secs = float(sys.argv[3]) if len(sys.argv) > 3 else 10
            p.exec_raw("import gc, os, sys\nfor _k in [k for k in globals() if k[:2] != '__' and k not in ('gc', 'os', 'sys')]:\n del globals()[_k]\nfor _f in os.listdir('/'):\n sys.modules.pop(_f[:-3], None)\ngc.collect()\n")
            out, err = p.exec_raw(open(sys.argv[2]).read(), secs)
            print(out, end='')
            if err:
                print('ERROR:\n' + err)
        elif cmd == 'deploy':
            for f in sorted(glob.glob(os.path.join(ROOT, 'firmware', '*.py'))):
                p.put(f, '/' + os.path.basename(f))
            html = os.path.join(ROOT, 'www', 'index.html')
            if os.path.exists(html):
                gz = gzip.compress(open(html, 'rb').read(), 9, mtime=0)
                p.put(html, '/www/index.html.gz', gz)
            sec = os.path.join(ROOT, 'secrets.py')
            if os.path.exists(sec):
                p.put(sec, '/secrets.py')
            else:
                print('note: no secrets.py, run tools/setup_wifi.py first')
        else:
            sys.exit(__doc__)
    finally:
        p.exit_raw()


if __name__ == '__main__':
    main()
