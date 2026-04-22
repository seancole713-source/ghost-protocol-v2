PYTHON ?= python3
PYTEST ?= $(PYTHON) -m pytest

.PHONY: test test-integration test-all test-compile test-e2e verify-live verify-health-audit check-error-signatures release-gates

test:
	$(PYTEST) -q -m "not integration"

test-integration:
	@if [ -z "$$TEST_DATABASE_URL" ] || [ "$$GHOST_INTEGRATION_TESTS" != "1" ]; then \
		echo "Integration tests require TEST_DATABASE_URL and GHOST_INTEGRATION_TESTS=1"; \
		exit 1; \
	fi
	$(PYTEST) -q -m integration

test-all:
	$(PYTEST) -q

test-compile:
	$(PYTHON) -m compileall -q .

test-e2e:
	npm run test:e2e

verify-live:
	npm run verify:live

verify-health-audit:
	$(PYTHON) scripts/verify_health_audit.py

check-error-signatures:
	$(PYTHON) scripts/check_error_signatures.py

release-gates: verify-live verify-health-audit check-error-signatures
	bash scripts/go-no-go.sh
