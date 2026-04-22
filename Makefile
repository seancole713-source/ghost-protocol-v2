# Makefile — Ghost Protocol v2
# Targets for running tests, type checks, and release gates locally.
#
# Usage:
#   make test              Run unit tests
#   make type-check        Run mypy on gate scripts
#   make gates             Run all release gate scripts against BASE_URL
#   make go-no-go          Run the bash gate orchestrator
#   make verify-audit      Run verify_health_audit.py
#   make check-signatures  Run check_error_signatures.py
#   make all-checks        Run tests + type-check + gates

.PHONY: test type-check gates go-no-go verify-audit check-signatures all-checks help

BASE_URL ?= https://ghost-protocol-v2-production.up.railway.app
CRON_SECRET ?=
PYTHON ?= python3

# ── Tests ─────────────────────────────────────────────────────────────────────

test:
	@echo "==> Running unit tests..."
	GHOST_TEST_MODE=1 pytest tests/ -v --tb=short

# ── Type checking ─────────────────────────────────────────────────────────────

type-check:
	@echo "==> Running mypy on gate scripts..."
	$(PYTHON) -m mypy scripts/verify_health_audit.py scripts/check_error_signatures.py \
		--config-file mypy.ini

# ── Release gates ─────────────────────────────────────────────────────────────

go-no-go:
	@echo "==> Running go/no-go gate orchestrator..."
	BASE_URL="$(BASE_URL)" CRON_SECRET="$(CRON_SECRET)" bash scripts/go-no-go.sh

verify-audit:
	@echo "==> Running verify_health_audit.py..."
	BASE_URL="$(BASE_URL)" CRON_SECRET="$(CRON_SECRET)" $(PYTHON) scripts/verify_health_audit.py

check-signatures:
	@echo "==> Running check_error_signatures.py..."
	BASE_URL="$(BASE_URL)" CRON_SECRET="$(CRON_SECRET)" $(PYTHON) scripts/check_error_signatures.py

gates: go-no-go verify-audit check-signatures
	@echo "==> All gate scripts complete."

# ── Combined ──────────────────────────────────────────────────────────────────

all-checks: test type-check gates
	@echo "==> All checks complete."

# ── Help ──────────────────────────────────────────────────────────────────────

help:
	@echo ""
	@echo "Ghost Protocol v2 — Make targets"
	@echo ""
	@echo "  make test              Run unit tests (pytest)"
	@echo "  make type-check        Run mypy on gate scripts"
	@echo "  make go-no-go          Run bash gate orchestrator"
	@echo "  make verify-audit      Run verify_health_audit.py"
	@echo "  make check-signatures  Run check_error_signatures.py"
	@echo "  make gates             Run all gate scripts"
	@echo "  make all-checks        Run tests + type-check + gates"
	@echo ""
	@echo "  Override BASE_URL and CRON_SECRET:"
	@echo "    make gates BASE_URL=https://... CRON_SECRET=mysecret"
	@echo ""
