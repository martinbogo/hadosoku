#!/usr/bin/env python3
"""Generate docs/pinout.svg: Pico W top view, highlighting the scope wiring."""
import os

LEFT = ['GP0', 'GP1', 'GND', 'GP2', 'GP3', 'GP4', 'GP5', 'GND', 'GP6', 'GP7', 'GP8', 'GP9', 'GND',
        'GP10', 'GP11', 'GP12', 'GP13', 'GND', 'GP14', 'GP15']          # pins 1..20, top to bottom
RIGHT = ['VBUS', 'VSYS', 'GND', '3V3_EN', '3V3 OUT', 'ADC_VREF', 'GP28 / ADC2', 'AGND', 'GP27 / ADC1',
         'GP26 / ADC0', 'RUN', 'GP22', 'GND', 'GP21', 'GP20', 'GP19', 'GP18', 'GND', 'GP17', 'GP16']  # pins 40..21

BG, BOARD, TXT, MUTED = '#0f1520', '#17613a', '#e6edf7', '#8b98b0'
Y0, DY = 118, 27
X_L, X_R = 292, 428


def kind(label):
    if label in ('GND', 'AGND'): return '#6b7280'
    if label in ('VBUS', 'VSYS', '3V3 OUT', 'ADC_VREF', '3V3_EN'): return '#ef4444'
    if label == 'RUN': return '#a78bfa'
    if 'ADC' in label: return '#22d3ee'
    return '#4f8cff'


o = []
w = o.append
w('<svg viewBox="0 0 720 756" xmlns="http://www.w3.org/2000/svg" font-family="-apple-system,Segoe UI,Helvetica,Arial,sans-serif">')
w('<title>Raspberry Pi Pico W pinout with Hadosoku wiring</title>')
w('<desc>Top view with USB at the top. Pin 20 (GP15) is the last pin on the left column. Pin 31 (GP26) is the tenth pin down the right column.</desc>')
w(f'<rect width="720" height="756" rx="14" fill="{BG}"/>')
w(f'<text x="360" y="30" text-anchor="middle" font-size="16" font-weight="700" fill="{TXT}">Pico W, top view (USB at top)</text>')
# board
w(f'<rect x="{X_L-22}" y="62" width="{X_R-X_L+44}" height="620" rx="10" fill="{BOARD}" stroke="#2fa564" stroke-width="2"/>')
w('<rect x="334" y="44" width="52" height="34" rx="4" fill="#9aa3b2" stroke="#cbd2de"/>')
w('<text x="360" y="66" text-anchor="middle" font-size="11" font-weight="700" fill="#10151f">USB</text>')
w('<rect x="330" y="98" width="60" height="22" rx="3" fill="#e8e8ec"/><text x="360" y="113" text-anchor="middle" font-size="9.5" font-weight="700" fill="#10151f">BOOTSEL</text>')
w('<rect x="334" y="260" width="52" height="52" rx="3" fill="#1b1b1f" stroke="#555"/><text x="360" y="290" text-anchor="middle" font-size="10" fill="#cbd2de">RP2040</text>')
w('<rect x="331" y="560" width="58" height="52" rx="3" fill="#b8bdc8" stroke="#e5e8ee"/><text x="360" y="582" text-anchor="middle" font-size="10" font-weight="700" fill="#10151f">Wi-Fi</text><text x="360" y="596" text-anchor="middle" font-size="9" fill="#10151f">CYW43</text>')
w('<path d="M338 640 h8 v-14 h8 v14 h8 v-14 h8 v14 h8 v-14 h8 v14 h8" fill="none" stroke="#e0b24a" stroke-width="2"/><text x="360" y="662" text-anchor="middle" font-size="9" fill="#d9e9df">antenna</text>')

pos = {}
for i, lab in enumerate(LEFT):
    pos[i + 1] = (X_L, Y0 + DY * i, lab, 'L')
for i, lab in enumerate(RIGHT):
    pos[40 - i] = (X_R, Y0 + DY * i, lab, 'R')

HI = {20: ('#ffd23f', 'GP15  test signal OUT'), 31: ('#31d7f0', 'GP26  CH1 input (ADC0)')}
for pin, (x, y, lab, side) in sorted(pos.items()):
    col = kind(lab)
    hi = HI.get(pin)
    if hi:
        w(f'<circle cx="{x}" cy="{y}" r="13" fill="none" stroke="{hi[0]}" stroke-width="3"/>')
    w(f'<rect x="{x-8}" y="{y-8}" width="16" height="16" rx="3" fill="{hi[0] if hi else col}"/>')
    ncol = '#10151f'
    tx = x + (22 if side == 'L' else -22)
    anchor = 'start' if side == 'L' else 'end'
    w(f'<text x="{tx}" y="{y+4}" text-anchor="{anchor}" font-size="11" font-weight="700" fill="#d9e9df">{pin}</text>')
    lx = x - 20 if side == 'L' else x + 20
    la = 'end' if side == 'L' else 'start'
    wt = '700' if hi else '500'
    fill = hi[0] if hi else TXT
    w(f'<text x="{lx}" y="{y+4}" text-anchor="{la}" font-size="13" font-weight="{wt}" fill="{fill}">{lab}</text>')

# callouts
x1, y1 = pos[20][0], pos[20][1]
x2, y2 = pos[31][0], pos[31][1]
w(f'<rect x="14" y="{y1-20}" width="104" height="40" rx="8" fill="#2a2410" stroke="#ffd23f"/><text x="66" y="{y1-3}" text-anchor="middle" font-size="12" font-weight="700" fill="#ffd23f">PIN 20</text><text x="66" y="{y1+13}" text-anchor="middle" font-size="10" fill="#e8d9a0">bottom left</text>')
w(f'<rect x="602" y="{y2-20}" width="104" height="40" rx="8" fill="#0f2a30" stroke="#31d7f0"/><text x="654" y="{y2-3}" text-anchor="middle" font-size="12" font-weight="700" fill="#31d7f0">PIN 31</text><text x="654" y="{y2+13}" text-anchor="middle" font-size="10" fill="#a6e8f3">right, 10th down</text>')
w(f'<text x="360" y="700" text-anchor="middle" font-size="13" font-weight="700" fill="#ffb59f">Jumper wire: pin 20 (GP15)  to  pin 31 (GP26)</text>')
# legend
ly = 730
items = [('#ffd23f', 'test out'), ('#31d7f0', 'ADC input'), ('#4f8cff', 'GPIO'), ('#ef4444', 'power'), ('#6b7280', 'GND'), ('#a78bfa', 'RUN')]
x = 66
for col, name in items:
    w(f'<rect x="{x}" y="{ly-9}" width="14" height="14" rx="3" fill="{col}"/><text x="{x+20}" y="{ly+3}" font-size="12" fill="{MUTED}">{name}</text>')
    x += 104
w('</svg>')
svg = '\n'.join(o)
os.makedirs(os.path.join(os.path.dirname(__file__), '..', 'docs'), exist_ok=True)
open(os.path.join(os.path.dirname(__file__), '..', 'docs', 'pinout.svg'), 'w').write(svg)
print(len(svg))
