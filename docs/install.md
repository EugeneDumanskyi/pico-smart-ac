# Install

From a new Pico 2 W to the page on your phone. About an hour, most of it waiting for builds. The commands are for a
Mac or Linux; `<ac-ip>`, `<pico-ip>` and `<port>` stand for your own values.

## Parts

- A Raspberry Pi Pico 2 W (no headers needed) and a USB cable that carries data, not a charge-only one.
- A USB power supply where the Pico will live (any phone charger). It only needs Wi-Fi to reach the AC.
- A Daikin room AC whose Wi-Fi adapter answers `POST /dsiot/multireq` on the local network
  ([DAIKIN_API.md](DAIKIN_API.md)). Tested with one unit (adapter firmware 3.6.0); other models may differ.
- A 2.4 GHz Wi-Fi network (the Pico has no 5 GHz) and a phone on the same network.

## Tools

On the computer: Python 3, [pipx](https://pipx.pypa.io/), git, make and a C compiler (on a Mac
`xcode-select --install`; on Debian or Ubuntu `sudo apt install build-essential`). Then:

```sh
git clone https://github.com/<owner>/pico-smart-ac.git
cd pico-smart-ac
make micropython   # the MicroPython Unix port and mpy-cross, built from the pinned release into ~/.cache/smartac/
```

`make micropython` takes a few minutes, once. `mpremote` (the USB tool) comes through `pipx run`, at the version
pinned in `tools/deploy.py`.

## Flash MicroPython

Follow [firmware.md, "Flashing"](firmware.md#flashing): download the pinned UF2, check its SHA-256, copy it onto the
Pico in BOOTSEL mode, and check the version with `pipx run mpremote==1.29.0 exec "import sys; print(sys.implementation)"`.
Use exactly that release; other versions are untested.

## The AC's address

1. Find the AC's adapter in the router's list of DHCP clients.
2. Give it a DHCP reservation there, so its address doesn't change after a router restart. Without one, the Pico
   loses the AC whenever the address moves (the page then shows "unreachable").
3. Check it from the computer (read-only): `python3 tools/daikin_status.py <ac-ip>` prints the room temperature,
   humidity and the AC's state.

## `secrets.py`

```sh
cp secrets.example.py secrets.py
```

Fill in (the file is gitignored and stays on your computer and the Pico):

| Name | Value |
|---|---|
| `WIFI_SSID`, `WIFI_PASSWORD` | a 2.4 GHz network (one SSID for both bands is fine) |
| `DAIKIN_IP` | `<ac-ip>` from above; the Settings tab can change it later |
| `API_TOKEN` | a long random string: `python3 -c "import secrets; print(secrets.token_urlsafe(24))"`. Without it the Pico refuses every change. |

## Deploy and find the Pico

With the Pico on USB:

```sh
make deploy
```

It compiles the code to `.mpy`, puts the Pico in maintenance mode, copies the code, the page and `secrets.py`, and
starts the app. Then it waits up to 30 s on the USB serial for the Pico's `smartac: wifi up <pico-ip>` line and prints
`http://<pico-ip>/`. If the line doesn't come (or with `--no-wait`), find the Pico in the router's DHCP client list.
Give the Pico a reservation too, so a bookmark on the phone keeps working.

`deploy.py` never writes or removes the Pico's own data (`*.json`, `events*.log`, `crash.txt`).
If mpremote doesn't find the board, name the port: `python3 tools/deploy.py --port <port>`
(`pipx run mpremote==1.29.0 connect list` lists them).

## First use

1. Open `http://<pico-ip>/` on the phone. The first change you make asks for `API_TOKEN`; the browser keeps it.
2. At first boot the active profile is `off`: the Pico reads the AC and records history, but starts nothing.
3. Turn `dry_run` on in the Settings tab, then pick a profile on the Status tab. For a week the Pico decides as it
   would, but only logs what it would send (`would` events). Check them on the History tab: the chart shows what
   the room did, the event list what the Pico would have done and on which reading.
4. Adjust the profiles on the Profiles tab if needed, then turn `dry_run` off.

The remote always wins: when someone changes the AC by hand, the Pico steps back (`manual` on the Status tab) and takes
over again later, as set in the Settings tab.

## The LED

| LED | Means |
|---|---|
| dark | all well |
| steady on | Wi-Fi is up but the AC doesn't answer |
| fast blink | Wi-Fi is down, or `secrets.py` is missing |

## Updating

```sh
git pull
make deploy
```

Profiles, settings and the event log stay. The 24 h chart starts over: it lives in RAM, and a deploy reboots the
Pico. Flash a new MicroPython only when [firmware.md](firmware.md) pins a new one.

## Backup

```sh
python3 tools/deploy.py --backup
```

copies the Pico's data files into `local/backup-<time>/`. It reboots the Pico twice (in and out of maintenance mode),
so the chart starts over.

## Maintenance mode

The file `/maint` on the Pico. At boot `main.py` sees it and stops before the app and its watchdog start, so files
can be copied. `deploy.py` sets and clears it itself; by hand: `python3 tools/deploy.py --maint` and `--run`.
`--stay` deploys and stays in it (for the device tests).

## Troubleshooting

To see what the Pico prints, attach to its serial port: `pipx run mpremote==1.29.0 repl` (Ctrl-] leaves). Don't press
Ctrl-C there: it stops the app, and the watchdog resets the board a few seconds later.

| Symptom | Check |
|---|---|
| LED blinks fast, no page | Wi-Fi: SSID and password in `secrets.py`, a 2.4 GHz network, signal where the Pico is. After 15 min without Wi-Fi the Pico resets itself and tries again. The serial port says `secrets.py missing` if it is. |
| Page doesn't load, LED dark | `/maint` left set (the serial port says so at boot): `python3 tools/deploy.py --run`. Or the Pico's address changed: the router's DHCP list. |
| "AC not reachable" on the Status tab, LED steady | The AC's address changed (no reservation?) or its adapter is offline. Settings tab: type the new address, **Test connection**, save. `python3 tools/daikin_status.py <ac-ip>` checks it from the computer. |
| "wrong token" or 401 | The token in the browser isn't `API_TOKEN`. The page forgets it and asks again. A changed `API_TOKEN` needs `make deploy`. |
| "flash write failed" | The Pico's flash is full. Make a backup, then ask for help with the output of `/api/state`. |
| Settings tab shows a reset you didn't do | `last crash` names the exception; `resets` counts each cause. `watchdog`, `wifi_down` and `ac_silent` are the Pico recovering by itself. |
