# Makefile for Execution Engine

.PHONY: help install test lint format type-check clean docker-build docker-run service-install service-status service-restart backup

help: ## Show this help message
	@echo 'Usage: make [target]'
	@echo ''
	@echo 'Targets:'
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  %-15s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

install: ## Install the package in development mode
	pip install -e .[dev]

test: ## Run the test suite
	pytest

test-unit: ## Run unit tests only
	pytest tests/unit/

test-integration: ## Run integration tests only
	pytest tests/integration/

lint: ## Lint code with ruff
	ruff check src/ tests/

format: ## Format code with ruff
	ruff format src/ tests/

type-check: ## Run mypy type checking
	mypy src/

check-env: ## Validate environment configuration
	python scripts/check_env.py

gen-secret: ## Generate WebSocket secret key
	python scripts/gen_secret.py

clean: ## Clean up build artifacts
	rm -rf build/ dist/ *.egg-info .coverage htmlcov/ .pytest_cache/

docker-build: ## Build Docker image
	docker build -t execution-engine .

docker-run: ## Run Docker container
	docker run --env-file .env -p 8080:8080 execution-engine

run: ## Run the application
	execution-engine

dev: ## Run in development mode with debug logging
	LOG_LEVEL=DEBUG execution-engine

pre-commit: ## Run pre-commit hooks
	pre-commit run --all-files

# Windows Service Management (NSSM)
service-install: ## Install as Windows service (run as Administrator)
	powershell -ExecutionPolicy Bypass -File install_service.ps1

service-status: ## Check Windows service status
	powershell -File scripts/service.ps1 status

service-logs: ## Monitor Windows service logs
	powershell -File scripts/service.ps1 logs

service-restart: ## Restart Windows service
	powershell -File scripts/service.ps1 restart

service-stop: ## Stop Windows service
	powershell -File scripts/service.ps1 stop

service-remove: ## Uninstall Windows service
	powershell -File scripts/service.ps1 remove

# Backup & Recovery
backup: ## Backup database and configuration
	powershell -File scripts/backup.ps1

# Default target
.DEFAULT_GOAL := help