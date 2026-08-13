.PHONY: help test install setup clean precommit-install precommit lint format typecheck

help: ## Show this help message
	@echo 'Usage: make [target]'
	@echo ''
	@echo 'Available targets:'
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  %-20s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

test: ## Run all tests
	@echo "🧪 Running all tests..."
	@uv run pytest -v

install: ## Install dependencies
	@echo "📦 Installing dependencies..."
	@uv sync --dev

setup: ## Setup project (install dependencies + pre-commit hooks)
	@echo "🚀 Setting up project..."
	@$(MAKE) install
	@$(MAKE) precommit-install
	@echo ""
	@echo "✅ Setup complete! You're ready to start developing."

lint: ## Run linting checks
	@echo "🔍 Running linting checks..."
	@uv run ruff format --check
	@uv run ruff check

format: ## Format code
	@echo "✨ Formatting code..."
	@uv run ruff format
	@uv run ruff check --fix

typecheck: ## Run type checking
	@echo "🔍 Running type checks..."
	@uv run pyright

precommit-install: ## Install pre-commit hooks
	@echo "🔧 Installing pre-commit hooks..."
	@uv run pre-commit install
	@echo "✅ Pre-commit hooks installed successfully!"
	@echo ""
	@echo "Pre-commit will now run automatically on every commit."
	@echo "To run manually: make precommit"

precommit: ## Run pre-commit hooks manually on all files
	@echo "🔍 Running pre-commit checks..."
	@uv run pre-commit run --all-files

clean: ## Clean up generated files and caches
	@echo "🧹 Cleaning up..."
	@rm -rf .pytest_cache
	@rm -rf .ruff_cache
	@rm -rf **/__pycache__
	@find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	@echo "✅ Cleanup complete"
