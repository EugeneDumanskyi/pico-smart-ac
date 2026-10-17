#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
# Build and deploy to the Pico over USB (architecture.md, "Secrets and personal settings"; README, "Setup").
#   python3 tools/deploy.py            build, deploy, start the app
#   python3 tools/deploy.py --build    only compile into build/
#   python3 tools/deploy.py --stay     deploy, stay in maintenance mode (device tests)
#   python3 tools/deploy.py --maint    only enter maintenance mode
#   python3 tools/deploy.py --run      only leave maintenance mode (start the app)
#   python3 tools/deploy.py --backup   copy the Pico's JSON, log and crash files into local/backup-<time>/
# Maintenance mode is the file /maint: main.py returns to the REPL before the watchdog starts, so the board stays
# quiet while files are copied. The app's own files (*.json, events*.log, crash.txt) are never written or removed.
# Needs mpy-cross from tools/build_micropython.sh (MPY_DIR), and mpremote (MPREMOTE, default pipx run, pinned).
import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
BUILD = ROOT / "build"
MPY_DIR = Path(os.environ.get("MPY_DIR", "~/.cache/smartac/micropython-v1.29.0")).expanduser()
MPY_CROSS = MPY_DIR / "mpy-cross" / "build" / "mpy-cross"
MPREMOTE = shlex.split(os.environ.get("MPREMOTE", "pipx run mpremote==1.29.0"))
TREES = ("smartac", "lib")  # under src/, compiled to .mpy
MANAGED = ("/smartac", "/lib", "/www")  # board dirs this tool owns; stale files in them are removed
APP_FILES = (".json", ".json.bak", ".json.tmp", ".log", "crash.txt")  # the app's; only --backup reads them

# The reset's reason for the next boot (board/pico.py). MicroPython doesn't close a file when its last reference
# goes, so every file is closed explicitly before machine.reset().
REASON = "f=open('/reset_reason.txt','w')\nf.write('deploy')\nf.close()\nimport machine\nmachine.reset()"
ENTER_MAINT = "open('/maint','w').close()\n" + REASON
LEAVE_MAINT = "import os\ntry:\n os.remove('/maint')\nexcept OSError:\n pass\n" + REASON
LIST = """
import os, json
def walk(d, out):
    try:
        entries = list(os.ilistdir(d))
    except OSError:
        return
    for e in entries:
        p = (d if d != '/' else '') + '/' + e[0]
        if e[1] == 0x4000:
            if d != '/':
                walk(p, out)
        else:
            out.append(p)
out = []
for d in %r:
    walk(d, out)
print('FILES' + json.dumps(out))
"""


class Pico:
    def __init__(self, port):
        self.prefix = MPREMOTE + (["connect", port] if port else [])

    def run(self, *args, check=True, capture=False):
        r = subprocess.run(self.prefix + list(args), capture_output=capture, text=True)
        if check and r.returncode:
            sys.exit("mpremote failed (%d): %s" % (r.returncode, " ".join(args[:3])))
        return r

    def chain(self, commands):
        # Several mpremote commands in one session ("a + b + c").
        args = []
        for c in commands:
            args += (["+"] if args else []) + c
        if args:
            self.run(*args)

    def reset_with(self, code):
        # The board resets in the middle of this exec, so mpremote loses the port and fails: that's expected.
        self.run("exec", code, check=False, capture=True)
        time.sleep(2)

    def enter_maint(self):
        self.reset_with(ENTER_MAINT)
        check = "import os\ntry:\n os.stat('/maint')\n print('MAINT yes')\nexcept OSError:\n print('MAINT no')"
        for _ in range(15):
            r = self.run("exec", check, check=False, capture=True)
            if r.returncode == 0 and "MAINT yes" in r.stdout:
                return
            if r.returncode == 0 and "MAINT no" in r.stdout:
                sys.exit("the Pico came back without /maint")
            time.sleep(1)
        sys.exit("the Pico did not come back after the reset")

    def leave_maint(self):
        # No exec afterwards: it would interrupt the app that just started.
        self.reset_with(LEAVE_MAINT)

    def files(self, dirs):
        r = self.run("exec", LIST % (tuple(dirs),), capture=True)
        for line in r.stdout.splitlines():
            if line.startswith("FILES"):
                return json.loads(line[5:])
        sys.exit("could not list the Pico's files")


def build():
    if not MPY_CROSS.exists():
        sys.exit("%s not found; run make micropython" % MPY_CROSS)
    shutil.rmtree(BUILD, ignore_errors=True)
    count = 0
    for tree in TREES:
        for src in sorted((SRC / tree).rglob("*.py")):
            rel = src.relative_to(SRC)
            out = BUILD / rel.with_suffix(".mpy")
            out.parent.mkdir(parents=True, exist_ok=True)
            # Run from src/ so tracebacks name smartac/shell/app.py, not a path on this Mac.
            subprocess.run([str(MPY_CROSS), "-o", str(out), str(rel)], cwd=SRC, check=True)
            count += 1
    (BUILD / "www").mkdir()
    shutil.copy(ROOT / "web" / "index.html", BUILD / "www" / "index.html")
    shutil.copy(SRC / "main.py", BUILD / "main.py")
    print("built %d modules into %s" % (count, BUILD.relative_to(ROOT)))


def deploy(pico, stay):
    secrets = ROOT / "secrets.py"
    if not secrets.exists():
        sys.exit("secrets.py missing: copy secrets.example.py and fill it in first")
    build()
    print("entering maintenance mode")
    pico.enter_maint()
    local = sorted("/" + p.relative_to(BUILD).as_posix() for p in BUILD.rglob("*") if p.is_file())
    copies = [["cp", str(BUILD / "main.py"), ":main.py"], ["cp", str(secrets), ":secrets.py"]]
    copies += [["cp", "-r", str(BUILD / d.strip("/")), ":"] for d in MANAGED]
    pico.chain(copies)
    stale = [p for p in pico.files(MANAGED) if p not in local]
    for p in stale:
        if p.endswith(APP_FILES):
            sys.exit("refusing to remove %s" % p)
    pico.chain([["rm", ":" + p] for p in stale])
    print("copied %d files, removed %d stale" % (len(local) + 1, len(stale)))
    if stay:
        print("staying in maintenance mode; tools/deploy.py --run starts the app")
    else:
        print("starting the app")
        pico.leave_maint()


def backup(pico):
    print("entering maintenance mode")
    pico.enter_maint()
    names = [p for p in pico.files(["/"]) if p.endswith(APP_FILES)]
    out = ROOT / "local" / ("backup-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%SZ"))
    out.mkdir(parents=True)
    pico.chain([["cp", ":" + p, str(out / p.lstrip("/"))] for p in names])
    print("%d files into %s" % (len(names), out.relative_to(ROOT)))
    pico.leave_maint()


def main():
    ap = argparse.ArgumentParser(description="Build and deploy smartac to the Pico.")
    ap.add_argument("--port", help="serial port (default: mpremote's auto-detect)")
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--build", action="store_true", help="only compile into build/")
    group.add_argument("--stay", action="store_true", help="deploy, then stay in maintenance mode")
    group.add_argument("--maint", action="store_true", help="only enter maintenance mode")
    group.add_argument("--run", action="store_true", help="only leave maintenance mode")
    group.add_argument("--backup", action="store_true", help="copy the app's files into local/")
    args = ap.parse_args()
    pico = Pico(args.port)
    if args.build:
        build()
    elif args.maint:
        pico.enter_maint()
        print("in maintenance mode")
    elif args.run:
        pico.leave_maint()
        print("app started")
    elif args.backup:
        backup(pico)
    else:
        deploy(pico, args.stay)


if __name__ == "__main__":
    main()
