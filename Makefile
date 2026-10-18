# SPDX-License-Identifier: GPL-3.0-only
# One entry point for the Mac and CI. See README.md, "Development".
RUFF_VERSION := 0.16.10
MPY_DIR ?= $(HOME)/.cache/smartac/micropython-v1.29.0
MICROPYTHON ?= $(MPY_DIR)/ports/unix/build-standard/micropython
SEED ?= 1
DEV_DIR ?= $(patsubst %/,%,$(or $(TMPDIR),/tmp))/smartac-dev
DEV_SPEED ?= 10
export MPY_DIR

.PHONY: micropython lint test month dev build deploy

micropython:
	tools/build_micropython.sh

lint:
	pipx run ruff==$(RUFF_VERSION) check .
	python3 tools/check_spdx.py

test:
	$(MICROPYTHON) tests/run.py

month:
	$(MICROPYTHON) sim/harness.py --days 30 --seed $(SEED) --quiet

dev:
	$(MICROPYTHON) sim/dev.py $(DEV_DIR) $(DEV_SPEED)

build:
	python3 tools/deploy.py --build

deploy:
	python3 tools/deploy.py
