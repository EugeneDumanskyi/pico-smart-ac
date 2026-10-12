# pico-smart-ac

A Raspberry Pi Pico 2 W that runs a Daikin room AC in short, purposeful cycles instead of leaving it on all night.
It cools when the room is hot, dries when it is humid, always dries the coil afterwards, then switches the AC off.
The remote always wins: when someone uses it, the Pico steps back.

**Status: design phase.** There is no firmware yet; see the [roadmap](docs/roadmap.md).

## How it works

- Profiles (`day`, `night`, `away`, `off`) hold simple rules: "humidity above 75% → dry for up to 60 min, then fan 20 min".
- The Pico reads the AC once a minute over the Wi-Fi adapter's local HTTP API. No cloud, no account.
- Every duration is relative; the Pico needs no clock or internet connection.
- A web page served by the Pico shows the status and edits profiles and settings.

## Hardware

- Raspberry Pi Pico 2 W with MicroPython (version pinned in [firmware.md](docs/firmware.md)).
- A Daikin room AC with a Wi-Fi adapter that answers `POST /dsiot/multireq` on the local network.
  Tested with one unit (adapter firmware 3.6.0); other models may differ.

## Docs

| Doc | Contents |
|---|---|
| [idea.md](docs/idea.md) | behaviour, profiles, safety, web UI, HTTP API |
| [architecture.md](docs/architecture.md) | modules, tasks, failure handling, testing |
| [roadmap.md](docs/roadmap.md) | milestones and decisions |
| [decisions/](docs/decisions/) | architecture decision records |
| [DAIKIN_API.md](docs/DAIKIN_API.md) | the AC's local API as far as we know it |
| [firmware.md](docs/firmware.md) | MicroPython version and vendored libraries |

## Setup

Not usable yet. The local configuration will be: copy `secrets.example.py` to `secrets.py` (gitignored) and fill in
your Wi-Fi, the AC's IP address and an API token.

To read the AC's status from a computer: `python3 tools/daikin_status.py <ac-ip>`.

## Development

Tests run on the MicroPython Unix port, built from the pinned release ([firmware.md](docs/firmware.md)).
Needs `git`, `make`, a C compiler, Python 3 and `pipx`.

```sh
make micropython   # build the Unix port once, into ~/.cache/smartac/
make lint          # ruff and the license header check
make test          # tests/run.py on the Unix port
make month         # the app against the simulator for 30 simulated days (SEED=1)
```

## License

[GPL-3.0-only](LICENSE). Vendored libraries keep their own licenses.

## Disclaimer

Not affiliated with or endorsed by Daikin. Uses the adapter's undocumented local API; use at your own risk.
