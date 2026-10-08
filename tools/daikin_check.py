# SPDX-License-Identifier: GPL-3.0-only
# Read-only check: can the Pico 2 W reach the Daikin AC's local API?
# Run with: mpremote run daikin_check.py  (copy secrets.example.py to secrets.py, fill it in, put it on the board)
import time
import network
import requests
import secrets

URL = "http://%s/dsiot/multireq" % secrets.DAIKIN_IP
QUERY = {"requests": [
    {"op": 2, "to": "/dsiot/edge.adp_i"},
    {"op": 2, "to": "/dsiot/edge.dev_i"},
]}

wlan = network.WLAN(network.STA_IF)
wlan.active(True)
wlan.connect(secrets.WIFI_SSID, secrets.WIFI_PASSWORD)
for _ in range(20):
    if wlan.isconnected():
        break
    time.sleep(1)
if not wlan.isconnected():
    raise SystemExit("Wi-Fi failed, status=%d" % wlan.status())
print("Pico IP:", wlan.ifconfig()[0])

r = requests.post(URL, json=QUERY, timeout=5)
print("HTTP", r.status_code)
data = r.json()
r.close()

def fields(resp):
    return {p["pn"]: p.get("pv") for p in resp["pc"]["pch"] if "pv" in p}

adp = fields(data["responses"][0])
dev = fields(data["responses"][1])
print("Daikin adapter fw:", adp.get("ver"))
print("Unit type:", dev.get("type"), "comm_err:", dev.get("comm_err"))
print("OK - Pico can talk to the AC")
