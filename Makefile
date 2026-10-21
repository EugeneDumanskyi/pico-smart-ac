# SPDX-License-Identifier: GPL-3.0-only
# One entry point for the Mac and CI. See README.md, "Development".
RUFF_VERSION := 0.16.10
MPY_DIR ?= $(HOME)/.cache/smartac/micropython-v1.29.0
MICROPYTHON ?= $(MPY_DIR)/ports/unix/build-standard/micropython
SEED ?= 1
DEV_DIR ?= $(patsubst %/,%,$(or $(TMPDIR),/tmp))/smartac-dev
DEV_SPEED ?= 10
M4_DIR ?= local/m4/$(shell date -u +%Y%m%d-%H%M%SZ)
MINUTE_S ?= 15
export MPY_DIR

.PHONY: micropython lint test month dev supervised web build deploy

micropython:
	tools/build_micropython.sh

lint:
	pipx run ruff==$(RUFF_VERSION) check .
	python3 tools/check_spdx.py

test:
	$(MICROPYTHON) tests/run.py

# The flash log lives in RAM here (MemoryStore): its 1300 small lines would fragment the default 2 MB heap.
month:
	$(MICROPYTHON) -X heapsize=8M sim/harness.py --days 30 --seed $(SEED) --quiet

# DEV_LIVE=1: not in shadow mode, the buttons drive real cycles on the fake AC.
dev:
	$(MICROPYTHON) sim/dev.py $(DEV_DIR) $(DEV_SPEED) $(if $(DEV_LIVE),--live)

# The page gzipped into build/www, with its size against the 60 KB budget.
web:
	python3 tools/build_web.py

# M4: DAIKIN_IP=<ac-ip> make supervised; SIM=1 for the simulator, M4_DIR=local/m4/<stamp> to restart a session.
supervised:
	$(MICROPYTHON) sim/supervised.py $(M4_DIR) --minute-s $(MINUTE_S) $(if $(SIM),--sim)

build:
	python3 tools/deploy.py --build

deploy:
	python3 tools/deploy.py
