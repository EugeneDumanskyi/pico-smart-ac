# SPDX-License-Identifier: GPL-3.0-only
# Copy to secrets.py (gitignored) and fill in. Never commit the real file. docs/install.md has the details.

# The Pico 2 W has 2.4 GHz Wi-Fi only: a 5 GHz-only network won't do. One SSID for both bands is fine.
WIFI_SSID = "your-ssid"
WIFI_PASSWORD = "your-password"

# The AC's Wi-Fi adapter. Give it a DHCP reservation in the router, so the address stays the same.
# Used until the Settings tab saves another one.
DAIKIN_IP = "192.0.2.10"

# Asked for once by the page before any change. Make a long random one:
#   python3 -c "import secrets; print(secrets.token_urlsafe(24))"
# Without it the Pico refuses every change.
API_TOKEN = "long-random-string"
