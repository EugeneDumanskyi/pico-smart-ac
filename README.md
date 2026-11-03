# pico-smart-ac

A Raspberry Pi Pico 2 W that runs a Daikin room AC in short, purposeful cycles instead of leaving it on all night.
It cools when the room is hot, dries when it is humid, always dries the coil afterwards, then switches the AC off.
The remote always wins: when someone uses it, the Pico steps back.

**Status: release candidate for v1.0 (M7).** Everything below is built, tested against the simulator and running on a
real AC; the 14-day soak comes before the tag. See the [roadmap](docs/roadmap.md).

## How it works

- Profiles hold simple rules: "humidity above 75% → cool for up to 40 min, then fan 20 min". `day` and `night` are
  schedules of time-of-day stages, each running its own rules; `away` and `turbo` (a quick cool-down on demand) are
  plain rule sets, and `off` starts nothing.
- The Pico reads the AC once a minute over the Wi-Fi adapter's local HTTP API. No cloud, no account.
- Every duration is relative. The wall clock only picks a schedule's stage; it comes from the AC's own status, with
  the phone's clock as a backup, so the Pico needs no internet connection.
- A web page served by the Pico, made for a phone, with four tabs:
  - **Status**: the room, the AC, what the Pico is doing; switch profile, pause, run a rule now, stop.
  - **History**: temperature, humidity and the AC's mode over 3, 12 or 24 h, and the events with the reading each
    decision was made on.
  - **Profiles**: edit the rules, with a plain-language summary of each.
  - **Settings**: the AC's address (with a connection test), timings, and the device's health.

## Hardware

- Raspberry Pi Pico 2 W with MicroPython (version pinned in [firmware.md](docs/firmware.md)).
- A Daikin room AC with a Wi-Fi adapter that answers `POST /dsiot/multireq` on the local network.
  Tested with one unit (adapter firmware 3.6.0); other models may differ.

## Setup

The [install guide](docs/install.md) has every step. In short:

1. Build the tools once (`make micropython`) and flash the pinned MicroPython
   ([firmware.md](docs/firmware.md#flashing)).
2. Give the AC a DHCP reservation, copy `secrets.example.py` to `secrets.py` and fill in a 2.4 GHz Wi-Fi network, the
   AC's address and a long random API token.
3. With the Pico on USB: `make deploy`. It prints the page's address, `http://<pico-ip>/`.
4. Open it on the phone, pick a profile, and run a week with `dry_run` on before letting it switch the AC.

## Docs

| Doc | Contents |
|---|---|
| [install.md](docs/install.md) | install, first use, updating, troubleshooting |
| [idea.md](docs/idea.md) | behaviour, profiles, safety, web UI, HTTP API |
| [architecture.md](docs/architecture.md) | modules, tasks, failure handling, testing |
| [roadmap.md](docs/roadmap.md) | milestones and decisions |
| [decisions/](docs/decisions/) | architecture decision records |
| [DAIKIN_API.md](docs/DAIKIN_API.md) | the AC's local API as far as we know it |
| [firmware.md](docs/firmware.md) | MicroPython version and vendored libraries |
| [m4-checklist.md](docs/m4-checklist.md) | the safety cases run on the real AC |

## Development

Tests run on the MicroPython Unix port, built from the pinned release ([firmware.md](docs/firmware.md)).
Needs `git`, `make`, a C compiler, Python 3 and `pipx`.

```sh
make micropython   # build the Unix port once, into ~/.cache/smartac/
make lint          # ruff and the license header check
make test          # tests/run.py on the Unix port
make month         # the app against the simulator for 30 simulated days (SEED=1)
make dev           # the page on http://127.0.0.1:8080 against the simulator (token dev, 10x time; DEV_LIVE=1: not shadow)
make web           # the page gzipped into build/www, checked against its 60 KB budget
make supervised    # M4: the app writing to the AC in DAIKIN_IP, each write approved (SIM=1: the simulator)
```

To read the AC's status from a computer: `python3 tools/daikin_status.py <ac-ip>`.

Deploy options (`python3 tools/deploy.py ...`):

| Option | Does |
|---|---|
| (none) | build, copy, start the app, wait for its address |
| `--build` | only compile into `build/` (`make build`) |
| `--stay` | deploy, then stay in maintenance mode |
| `--maint` / `--run` | enter / leave maintenance mode |
| `--backup` | copy the Pico's data files into `local/backup-<time>/` |
| `--port <port>` | the serial port, if mpremote doesn't find it |
| `--no-wait` | don't wait for the address after starting the app |

**Maintenance mode** is the file `/maint` on the Pico: at boot, `main.py` sees it and returns to the REPL before the
app and its watchdog start, so files can be copied and device tests run (`mpremote run tests/device/<script>.py`).

**Soak log**: `python3 tools/soak_log.py <pico-ip>` writes `/api/state` once a minute to a CSV in `local/`. The Pico
also writes its free heap and largest block to `events.log` once an hour (over 9 days kept), so a long soak needs no
computer: afterwards `tools/deploy.py --backup` copies the log off.

**Shadow review**: `python3 tools/shadow_pull.py <pico-ip>` saves the Pico's flash event log (`GET /api/log`), its
state and the 24 h history into `local/shadow/<time>/`; run it once a day. `--report` merges every pull and prints each
decision with its local time, the reading it was made on and the room from 10 min before to 30 min after.

**Supervised writes** (M4): `make supervised` runs the app on the Mac against the real AC, with every write approved
at the terminal and read back; [m4-checklist.md](docs/m4-checklist.md) lists the cases.

## License

[GPL-3.0-only](LICENSE). Vendored libraries keep their own licenses.

## Disclaimer

Not affiliated with or endorsed by Daikin. Uses the adapter's undocumented local API; use at your own risk.
