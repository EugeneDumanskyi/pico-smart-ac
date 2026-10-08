#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-only
# Build the MicroPython Unix port from the pinned release (docs/firmware.md), outside the repo.
# Usage: tools/build_micropython.sh   (MPY_DIR overrides the checkout location; prints the binary path)
set -eu

TAG=v1.29.0
COMMIT=0fd6c573ea815774668bbb16b8e197c8822368b2
MPY_DIR="${MPY_DIR:-$HOME/.cache/smartac/micropython-$TAG}"
BIN="$MPY_DIR/ports/unix/build-standard/micropython"
# Same flags on the Mac and in CI. FFI off: no libffi needed.
MAKE_FLAGS="MICROPY_PY_FFI=0"

if [ ! -d "$MPY_DIR/.git" ]; then
    mkdir -p "$(dirname "$MPY_DIR")"
    git clone --quiet --depth 1 --branch "$TAG" https://github.com/micropython/micropython.git "$MPY_DIR"
fi

head="$(git -C "$MPY_DIR" rev-parse HEAD)"
if [ "$head" != "$COMMIT" ]; then
    echo >&2 "build_micropython: $MPY_DIR is at $head, expected $TAG ($COMMIT). Remove it and rerun."
    exit 1
fi

if [ -x "$BIN" ]; then
    echo "$BIN"
    exit 0
fi

jobs="$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 2)"
make -C "$MPY_DIR/mpy-cross" -j "$jobs"
make -C "$MPY_DIR/ports/unix" submodules $MAKE_FLAGS
make -C "$MPY_DIR/ports/unix" -j "$jobs" $MAKE_FLAGS
echo "$BIN"
