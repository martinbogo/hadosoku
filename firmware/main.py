import sys
import time

import machine
from machine import mem32

WATCHDOG_CTRL = 0x40058000


def watchdog_off():
    # Ctrl-C means a person (or the deploy tool) is at the console: stop the
    # watchdog so it does not reset the board underneath them.
    mem32[WATCHDOG_CTRL] &= ~(1 << 30)


try:
    import net
    ip, mode, wlan = net.connect()
    print('wifi', mode, ip)
    import asyncio
    import server
    asyncio.run(server.main(ip, mode, wlan))
except KeyboardInterrupt:
    watchdog_off()
except Exception as e:
    sys.print_exception(e)
    time.sleep(3)
    machine.reset()
