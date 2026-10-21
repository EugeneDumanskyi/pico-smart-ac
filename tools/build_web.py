#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
# The page's assets for the Pico: each web/ file as it is plus a gzip -9 copy (mtime 0, so a build gives the same
# bytes every time), which shell/api.py serves with Content-Encoding: gzip. Prints the sizes and fails when the
# gzipped total is over the budget (roadmap M5). No minifier: the gzipped source is well under it.
#   python3 tools/build_web.py [out_dir]     default build/www
import gzip
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB_FILES = ("index.html", "editor.js", "history.js")  # served by shell/api.py's routes, nothing else
BUDGET = 60 * 1024


def build(out):
    # -> the gzipped total in bytes; exits when it is over BUDGET.
    out.mkdir(parents=True, exist_ok=True)
    total = 0
    for name in WEB_FILES:
        data = (ROOT / "web" / name).read_bytes()
        packed = gzip.compress(data, compresslevel=9, mtime=0)
        shutil.copy(ROOT / "web" / name, out / name)
        (out / (name + ".gz")).write_bytes(packed)
        total += len(packed)
        print("%-12s %6d B, gzipped %6d B" % (name, len(data), len(packed)))
    print("web total gzipped %d B of %d B" % (total, BUDGET))
    if total > BUDGET:
        sys.exit("the page is over its %d KB budget" % (BUDGET // 1024))
    return total


if __name__ == "__main__":
    build(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "build" / "www")
