# SPDX-License-Identifier: GPL-3.0-only
# Entry point on the Pico (architecture.md, "Secrets and personal settings"; README, "Setup").
# - /maint exists: return to the REPL before anything starts. The watchdog can't be stopped once started, so this is
#   how tools/deploy.py and the device tests get a quiet board.
# - secrets.py missing: blink the LED fast, say so on the serial port, no Wi-Fi.
# - Otherwise the app in observe mode (M1): it reads the AC and never writes.
import os


def _exists(path):
    try:
        os.stat(path)
        return True
    except OSError:
        return False


def main():
    if _exists("/maint"):
        print("smartac: /maint is set, not starting (tools/deploy.py --run clears it)")
        return
    from smartac.board.pico import PicoBoard

    try:
        import secrets
    except ImportError:
        PicoBoard().led("error")
        print("smartac: secrets.py missing; copy secrets.example.py, fill it in and deploy again")
        return
    from smartac.shell import tasks
    from smartac.shell.app import App
    from smartac.shell.store import FileStore

    board = PicoBoard(secrets)
    store = FileStore("/")
    print("smartac: boot (%s)" % board.reset_cause())
    tasks.run(board, store, lambda: App(board, store, getattr(secrets, "DAIKIN_IP", None), observe=True))


main()
