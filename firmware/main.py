import sys

try:
    import net
    ip, mode, wlan = net.connect()
    print('wifi', mode, ip)
    import asyncio
    import server
    asyncio.run(server.main(ip, mode, wlan))
except KeyboardInterrupt:
    pass
except Exception as e:
    sys.print_exception(e)
