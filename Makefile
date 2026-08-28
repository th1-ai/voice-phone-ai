# Makefile - everything you need to run this agent.
# Run `make` on its own to see the list.
#
# Nothing here needs credentials except `run` against real systems: `make setup`
# and `make demo` work on a fresh clone with an empty .env.

PYTHON ?= python3
VENV   := .venv
BIN    := $(VENV)/bin
PY     := $(BIN)/python
PIP    := $(BIN)/pip

.DEFAULT_GOAL := help
.PHONY: help setup doctor demo run watch review test schedule report clean

help:  ## show this list
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  make %-10s %s\n", $$1, $$2}'

setup:  ## create the virtualenv, install dependencies, copy example config
	@$(PYTHON) -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
		|| (echo 'This agent needs Python 3.11 or newer. Found:' && $(PYTHON) -V && exit 1)
	@test -d $(VENV) || $(PYTHON) -m venv $(VENV)
	@$(PIP) install --quiet --upgrade pip
	@$(PIP) install --quiet -r requirements.txt
	@test -f .env || (cp .env.example .env && echo '  created .env from .env.example')
	@for f in config/*.example.yaml; do \
		target=$${f%.example.yaml}.yaml; \
		test -f $$target || (cp $$f $$target && echo "  created $$target"); \
	done
	@mkdir -p data/logs data/exports data/imports data/pending
	@echo ''
	@echo 'Setup done. Next:  make demo    (runs the whole loop on sample data)'

doctor:  ## check config, credentials and every connected system
	@$(PY) tools/doctor.py

demo:  ## run one full cycle on the bundled fixtures - no credentials needed
	@$(PY) tools/demo.py

run:  ## one real pass (add ARGS='--limit 5 --dry-run' to change it)
	@$(PY) tools/run.py --once $(ARGS)

watch:  ## keep running on the configured interval until you stop it
	@$(PY) tools/run.py --watch $(ARGS)

review:  ## show what is waiting for a human (approve / edit / reject)
	@$(PY) tools/review.py list $(ARGS)

test:  ## run the test suite (no network, no credentials)
	@$(PY) -m pytest -q

schedule:  ## print a cron / launchd / systemd snippet for this machine
	@$(PY) tools/schedule.py $(ARGS)

report:  ## what the agent did, and what it cost
	@$(PY) tools/report.py $(ARGS)

clean:  ## delete runtime state (database, logs, exports, demo). Keeps data/imports, config and .env.
	@find data -mindepth 1 -maxdepth 1 ! -name imports -exec rm -rf {} + 2>/dev/null || true
	@rm -rf .pytest_cache **/__pycache__ */__pycache__ __pycache__
	@echo "Runtime state cleared (database, logs, exports, pending, demo). Kept: data/imports/, config/, .env."
	@echo "To drop your imported CSVs too: rm -rf data/imports"
