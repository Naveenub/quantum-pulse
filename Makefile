# QUANTUM-PULSE Makefile
# Usage: make <target>

.PHONY: help install dev-install test test-unit test-api bench lint format typecheck \
        run docker-build docker-up docker-down clean logs keygen health

PYTHON     := python3
PIP        := pip3
APP        := main:app
PORT       := 8747
PASSPHRASE ?= change-me-in-production-minimum-16-chars

## ── Help ──────────────────────────────────────────────────────────────────── ##

help:  ## Show this help
	@awk 'BEGIN{FS=":.*##"} /^[a-zA-Z_-]+:.*##/{printf "  \033[36m%-18s\033[0m %s\n",$$1,$$2}' $(MAKEFILE_LIST)

## ── Install ───────────────────────────────────────────────────────────────── ##

install:  ## Install production dependencies
	$(PIP) install -r requirements.txt

dev-install:  ## Install dev + test dependencies
	$(PIP) install -r requirements.txt pytest pytest-asyncio httpx mypy ruff pre-commit pytest-cov

## ── Testing ───────────────────────────────────────────────────────────────── ##

test:  ## Run full test suite (unit + API)
	QUANTUM_PASSPHRASE="$(PASSPHRASE)" QUANTUM_API_KEYS="test-key" \
	$(PYTHON) -m pytest tests/ -v --asyncio-mode=auto

test-unit:  ## Run unit tests only
	QUANTUM_PASSPHRASE="$(PASSPHRASE)" QUANTUM_API_KEYS="test-key" \
	$(PYTHON) -m pytest tests/test_engine.py -v --asyncio-mode=auto

test-api:  ## Run API integration tests only
	QUANTUM_PASSPHRASE="$(PASSPHRASE)" QUANTUM_API_KEYS="test-key" \
	$(PYTHON) -m pytest tests/test_api.py -v --asyncio-mode=auto

test-cov:  ## Run tests with coverage report
	QUANTUM_PASSPHRASE="$(PASSPHRASE)" QUANTUM_API_KEYS="test-key" \
	$(PYTHON) -m pytest tests/ --cov=core --cov=models --cov-report=html --cov-report=term-missing

## ── Benchmark ─────────────────────────────────────────────────────────────── ##

bench:  ## Run compression + pipeline benchmark
	QUANTUM_PASSPHRASE="$(PASSPHRASE)" $(PYTHON) scripts/benchmark_demo.py

## ── Code quality ──────────────────────────────────────────────────────────── ##

lint:  ## Lint with ruff
	$(PYTHON) -m ruff check core/ models/ main.py cli.py

format:  ## Format with ruff
	$(PYTHON) -m ruff format core/ models/ main.py cli.py

typecheck:  ## Type-check with mypy
	$(PYTHON) -m mypy core/ models/ --ignore-missing-imports

## ── Run ───────────────────────────────────────────────────────────────────── ##

run:  ## Run API server (development mode)
	QUANTUM_PASSPHRASE="$(PASSPHRASE)" QUANTUM_ENVIRONMENT=development \
	$(PYTHON) main.py

run-prod:  ## Run API server (production mode — set QUANTUM_PASSPHRASE env var first)
	QUANTUM_ENVIRONMENT=production \
	$(PYTHON) -m uvicorn $(APP) \
		--host 0.0.0.0 --port $(PORT) \
		--loop uvloop --http httptools \
		--workers 4 --access-log

## ── Docker ────────────────────────────────────────────────────────────────── ##

docker-build:  ## Build Docker image
	docker build -t quantum-pulse:latest .

docker-up:  ## Start MongoDB + QUANTUM-PULSE via docker-compose
	docker-compose up -d

docker-down:  ## Stop all containers
	docker-compose down

docker-logs:  ## Tail container logs
	docker-compose logs -f quantum-pulse

## ── CLI ───────────────────────────────────────────────────────────────────── ##

keygen:  ## Generate a strong passphrase
	QUANTUM_PASSPHRASE="placeholder-16-chars" $(PYTHON) cli.py keygen

health:  ## Query /healthz on running server
	QUANTUM_PASSPHRASE="placeholder-16-chars" $(PYTHON) cli.py health --host http://localhost:$(PORT)

## ── Maintenance ───────────────────────────────────────────────────────────── ##

clean:  ## Remove pycache, test artifacts, logs
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc"     -delete 2>/dev/null || true
	rm -rf .pytest_cache .mypy_cache htmlcov .coverage
	rm -f logs/*.log logs/*.jsonl

logs:  ## Tail live application logs
	tail -f logs/quantum_pulse.log

audit:  ## Tail live audit log
	tail -f logs/audit.jsonl
