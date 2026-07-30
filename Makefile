.PHONY: lint format typecheck test test-unit test-all test-live coverage metrics clean check

lint:
	ruff check .

format:
	ruff format .

# Fails loudly if mypy is absent rather than skipping — a gate that silently
# does nothing is worse than no gate. `pip install -e '.[dev]'` provides it.
typecheck:
	@mypy --version >/dev/null 2>&1 || { \
	  echo "mypy is not runnable (missing, or a stale shim on PATH whose"; \
	  echo "package is gone — \`command -v mypy\` passes for those). Install"; \
	  echo "the dev extras into the active environment:"; \
	  echo "    pip install -e '.[dev]'"; exit 1; }
	mypy .

# Default suite: everything except `live` (see pyproject.toml — live tests hit
# the county's server and need a human to clear the CAPTCHA).
test:
	pytest

test-unit:
	pytest -m unit

test-all:
	pytest -m ""

# Opt-in only, and never from the pipeline. Needs a CAPTCHA-cleared
# ./.browser_profile; run `make test-live` by hand when re-probing the portal.
test-live:
	pytest -m live

coverage:
	pytest --cov=. --cov-report=term-missing --cov-report=html

# Deterministic project-health numbers (see scripts/collection_metrics.py).
# Safe with no sjc.db — reports "no data yet" rather than failing.
metrics:
	python3 scripts/collection_metrics.py

clean:
	find . -type d -name __pycache__ -exec rm -rf {} +
	rm -rf htmlcov .coverage .pytest_cache .mypy_cache .ruff_cache
	rm -rf debug/
	rm -f *.log

# CI-style gate: everything must pass, nothing auto-fixed.
check: lint
	ruff format --check .
	$(MAKE) typecheck
	pytest -m "not live"
