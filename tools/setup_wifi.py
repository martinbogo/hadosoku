#!/usr/bin/env python3
"""Write secrets.py (Wi-Fi credentials) next to this repo. Run it yourself in a
terminal: the password is read with getpass and never echoed or logged."""
import getpass
import os

root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ssid = input('Wi-Fi SSID: ').strip()
pw = getpass.getpass('Wi-Fi password (hidden): ')
path = os.path.join(root, 'secrets.py')
with open(path, 'w') as f:
    f.write('SSID = %r\nPASSWORD = %r\n' % (ssid, pw))
os.chmod(path, 0o600)
print('Wrote', path)
