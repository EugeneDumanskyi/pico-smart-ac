#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
# Check that every tracked source file starts with the SPDX license line (docs/architecture.md).
# Run from anywhere inside the repo: python3 tools/check_spdx.py
import subprocess
import sys

SPDX = "SPDX-License-Identifier: GPL-3.0-only"
EXTENSIONS = (".py", ".js", ".css", ".html", ".sh")
VENDORED = ("src/lib/", "tests/lib/")


def git(*args):
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout


def tracked_files(root):
    # Tracked plus new, not ignored: the check also covers files not committed yet.
    out = git("-C", root, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
    return sorted(set(p for p in out.split("\0") if p))


def has_header(path):
    with open(path, encoding="utf-8") as f:
        lines = [f.readline() for _ in range(2)]
    if lines[0].startswith("#!"):
        lines = lines[1:]
    return any(SPDX in line for line in lines)


def main():
    root = git("rev-parse", "--show-toplevel").strip()
    missing = []
    for path in tracked_files(root):
        if not path.endswith(EXTENSIONS) or path.startswith(VENDORED):
            continue
        if not has_header(root + "/" + path):
            missing.append(path)
    for path in missing:
        print(f"missing SPDX header: {path}")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
