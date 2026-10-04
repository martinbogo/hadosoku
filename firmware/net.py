"""Wi-Fi bring-up: join the network from secrets.py, else fall back to an AP."""
import network
import time

HOSTNAME = 'hadosoku'


def connect(timeout=25):
    network.hostname(HOSTNAME)
    try:
        import secrets
        ssid, password = secrets.SSID, secrets.PASSWORD
    except (ImportError, AttributeError):
        ssid = password = None
    if ssid:
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
