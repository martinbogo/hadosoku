"""Wi-Fi bring-up: join the network from secrets.py, else fall back to an AP."""
import asyncio
import machine
import network
import time

HOSTNAME = 'hadosoku'
_creds = None


def connect(timeout=25):
    network.hostname(HOSTNAME)
    try:
        import secrets
        ssid, password = secrets.SSID, secrets.PASSWORD
    except (ImportError, AttributeError):
        ssid = password = None
    global _creds
    if ssid:
        _creds = (ssid, password)
        wlan = network.WLAN(network.STA_IF)
        wlan.active(True)
        wlan.config(pm=0xA11140)        # no power save: lower latency
        wlan.connect(ssid, password)
        t0 = time.ticks_ms()
        while time.ticks_diff(time.ticks_ms(), t0) < timeout * 1000:
            if wlan.status() == 3:      # CYW43 LINK_UP
                return wlan.ifconfig()[0], 'station', wlan
            if wlan.status() < 0:       # join failed / bad auth
                break
            time.sleep_ms(200)
        wlan.active(False)
    ap = network.WLAN(network.AP_IF)
    ap.config(ssid='Hadosoku', password='hadosoku')
    ap.active(True)
    while not ap.active():
        time.sleep_ms(100)
    return ap.ifconfig()[0], 'ap', ap


async def supervise(wlan, mode):
    """Reconnect if the Wi-Fi link drops; reboot if it cannot be recovered."""
    if mode != 'station':
        return
    down = 0
    while True:
        await asyncio.sleep(5)
        if wlan.isconnected():
            down = 0
            continue
        down += 1
        print('wifi down for', down * 5, 's')
        if down % 3 == 0 and _creds:
            try:
                wlan.disconnect()
                wlan.connect(*_creds)
            except Exception as e:
                print('wifi reconnect failed:', e)
        if down >= 24:                  # two minutes without a link
            machine.reset()
