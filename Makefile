# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

.PHONY: help test install install-deps setup start ui doctor clean precommit-install precommit lint format typecheck

help: ## Show this help message
	@echo 'Usage: make [target]'
	@echo ''
	@echo 'Available targets:'
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  %-20s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

start: ## One-click start Artemis Showcase UI and auto-open browser
	@bash start.sh

ui: ## Launch the unified Showcase UI & Admin Console in browser
	@uv run artemis ui --open

doctor: ## Run system, device, and toolchain diagnostics
	@uv run artemis doctor

test: ## Run all tests
	@echo "🧪 Running all tests..."
	@uv run pytest -v

install: ## Install python dependencies via uv
	@echo "📦 Installing python dependencies..."
	@uv sync --dev

install-deps: ## One-click install all system dependencies (ADB, FFmpeg, scrcpy, Python, uv)
	@echo "⚡ Running one-click dependency installer..."
	@bash scripts/install_deps.sh

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

clean: ## Clean up generated files, caches, and traces
	@echo "🧹 Cleaning up caches and temporary files..."
	@rm -rf .pytest_cache .ruff_cache .artemis_paused traces scratch .venv apps/showcase_ui/.angular build dist
	@find . -type d -name "outputs" -exec rm -rf {} + 2>/dev/null || true
	@find . -type f -name ".artemis_paused" -delete 2>/dev/null || true
	@find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	@find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	@find . -type f -name "*.py[cod]" -delete 2>/dev/null || true
	@echo "✅ Cleanup complete"
