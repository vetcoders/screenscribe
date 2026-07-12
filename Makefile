# Screenscribe Makefile
# Uses uv as package manager

.PHONY: help install dev setup-hooks lint format format-check build-check ship-verify verify verify-seed test test-unit test-integration test-all test-cov typecheck security check leak-scan brand-scan secrets-check precommit-check release-check run clean version version-patch version-minor version-major analyze e2e-review commit-safe test-race-protection

# Interpreter for the standalone ss-verify driver. uv is already a hard
# requirement of the whole verify flow (ss-verify orchestrates `uv run`
# internally), and `uv run python` guarantees a Python on every platform/CI
# runner without depending on a bare `python` on PATH. Override with
# `make verify PYTHON=python3` if you prefer the system interpreter.
PYTHON ?= uv run python

# Default target
# Help colors
HELP_C_CYAN   := \033[36m
HELP_C_GREEN  := \033[32m
HELP_C_YELLOW := \033[33m
HELP_C_RESET  := \033[0m

help:
	@printf '\n$(HELP_C_CYAN)%s$(HELP_C_RESET)\n' 'Screenscribe - Video Review Automation'
	@printf '\n'
	@printf '%s\n' 'Usage: make [target]'
	@printf '\n'
	@printf '  $(HELP_C_YELLOW)%s$(HELP_C_RESET)\n' 'SETUP'
	@printf '    $(HELP_C_GREEN)%-18s$(HELP_C_RESET) %s\n' 'install' 'Install CLI for normal use'
	@printf '    $(HELP_C_GREEN)%-18s$(HELP_C_RESET) %s\n' 'dev' 'Install dev dependencies + git hooks'
	@printf '    $(HELP_C_GREEN)%-18s$(HELP_C_RESET) %s\n' 'setup-hooks' 'Install pre-commit/pre-push hooks only'
	@printf '\n'
	@printf '  $(HELP_C_YELLOW)%s$(HELP_C_RESET)\n' 'QUALITY'
	@printf '    $(HELP_C_GREEN)%-18s$(HELP_C_RESET) %s\n' 'lint' 'Run linter (ruff check)'
	@printf '    $(HELP_C_GREEN)%-18s$(HELP_C_RESET) %s\n' 'format' 'Format code (ruff format)'
	@printf '    $(HELP_C_GREEN)%-18s$(HELP_C_RESET) %s\n' 'typecheck' 'Run type checker (mypy)'
	@printf '    $(HELP_C_GREEN)%-18s$(HELP_C_RESET) %s\n' 'security' 'Run security checks (bandit)'
	@printf '    $(HELP_C_GREEN)%-18s$(HELP_C_RESET) %s\n' 'check' 'Run all quality checks'
	@printf '    $(HELP_C_GREEN)%-18s$(HELP_C_RESET) %s\n' 'leak-scan' 'Scan tracked tree for secrets, tokens, local paths, artifacts'
	@printf '    $(HELP_C_GREEN)%-18s$(HELP_C_RESET) %s\n' 'brand-scan' 'Guard public-surface paths against ScreenScribe/VetCoders brand regressions'
	@printf '    $(HELP_C_GREEN)%-18s$(HELP_C_RESET) %s\n' 'secrets-check' 'detect-secrets scan against .secrets.baseline'
	@printf '    $(HELP_C_GREEN)%-18s$(HELP_C_RESET) %s\n' 'verify' 'THE gate: ss-verify READY/NOT READY (one source of truth)'
	@printf '    $(HELP_C_GREEN)%-18s$(HELP_C_RESET) %s\n' 'verify-seed' 'ss-verify the git-archive HEAD export (shippable tree)'
	@printf '    $(HELP_C_GREEN)%-18s$(HELP_C_RESET) %s\n' 'release-check' 'Pre-release gate (thin shim -> make verify)'
	@printf '\n'
	@printf '  $(HELP_C_YELLOW)%s$(HELP_C_RESET)\n' 'TESTING'
	@printf '    $(HELP_C_GREEN)%-18s$(HELP_C_RESET) %s\n' 'test' 'Run unit tests (default)'
	@printf '    $(HELP_C_GREEN)%-18s$(HELP_C_RESET) %s\n' 'test-unit' 'Run unit tests only'
	@printf '%s\n' '  test-integration Run integration tests (uses config or LIBRAXIS_API_KEY)'
	@printf '    $(HELP_C_GREEN)%-18s$(HELP_C_RESET) %s\n' 'test-all' 'Run all tests including integration'
	@printf '    $(HELP_C_GREEN)%-18s$(HELP_C_RESET) %s\n' 'test-cov' 'Run tests with coverage report'
	@printf '    $(HELP_C_GREEN)%-18s$(HELP_C_RESET) %s\n' 'test-race-protection' 'Living Tree commit-safe self-test'
	@printf '\n'
	@printf '  $(HELP_C_YELLOW)%s$(HELP_C_RESET)\n' 'COMMANDS'
	@printf '    $(HELP_C_GREEN)%-18s$(HELP_C_RESET) %s\n' 'analyze' 'Start interactive video analysis server'
	@printf '\n'
	@printf '  $(HELP_C_YELLOW)%s$(HELP_C_RESET)\n' 'VERSIONING'
	@printf '    $(HELP_C_GREEN)%-18s$(HELP_C_RESET) %s\n' 'version' 'Show current version'
	@printf '    $(HELP_C_GREEN)%-18s$(HELP_C_RESET) %s\n' 'version-patch' 'Bump patch version (0.1.2 -> 0.1.3)'
	@printf '    $(HELP_C_GREEN)%-18s$(HELP_C_RESET) %s\n' 'version-minor' 'Bump minor version (0.1.2 -> 0.2.0)'
	@printf '    $(HELP_C_GREEN)%-18s$(HELP_C_RESET) %s\n' 'version-major' 'Bump major version (0.1.2 -> 1.0.0)'
	@printf '\n'
	@printf '  $(HELP_C_YELLOW)%s$(HELP_C_RESET)\n' 'OTHER'
	@printf '    $(HELP_C_GREEN)%-18s$(HELP_C_RESET) %s\n' 'clean' 'Remove cache and build artifacts'
	@printf '    $(HELP_C_GREEN)%-18s$(HELP_C_RESET) %s\n' 'run' 'Run Screenscribe CLI'
	@printf '    $(HELP_C_GREEN)%-18s$(HELP_C_RESET) %s\n' 'commit-safe' 'Race-protected commit (MSG=.. FILES=..)'

# ============================================================================
# Setup
# ============================================================================

install:
	@printf '%s\n' '[1/3] Checking installer prerequisites...'
	@command -v uv >/dev/null 2>&1 || { \
		printf '%s\n' 'Error: uv is required. Install it first and retry.'; \
		exit 1; \
	}
	@printf '%s\n' '[2/3] Installing screenscribe and runtime dependencies...'
	@PATH="$$(uv tool dir --bin):$$PATH" uv tool install . --reinstall --force
	@printf '%s\n' '[3/3] Checking command availability...'
	@if command -v screenscribe >/dev/null 2>&1; then \
		printf '%s\n' 'Ready: screenscribe is installed and available on PATH.'; \
	else \
		TOOL_BIN=$$(uv tool dir --bin); \
		printf '%s\n' 'Installed successfully, but the uv tool bin directory is not on PATH.'; \
		printf 'Run: uv tool update-shell\nThen restart your terminal. Tool directory: %s\n' "$$TOOL_BIN"; \
	fi

dev: setup-hooks
	uv sync --dev

# Editable local CLI: bare `screenscribe` reflects this checkout live (no
# rebuild needed), so what you run locally is always the current code. Run
# `make ship-verify` before release to test the actual packaged wheel instead.
dev-link: setup-hooks
	uv sync --dev
	uv tool install -e . --force
	@printf '%s\n' 'Linked: bare `screenscribe` now runs this checkout live (editable). `screenscribe --version` shows +g<sha>.'

setup-hooks:
	@if [ -f .pre-commit-config.yaml ]; then \
		echo "Installing pre-commit hooks..."; \
		GLOBAL_HOOKS=$$(git config --global --get core.hooksPath 2>/dev/null || true); \
		if [ -n "$$GLOBAL_HOOKS" ]; then \
			echo "WARNING: a GLOBAL core.hooksPath is set ($$GLOBAL_HOOKS) — it shadows .git/hooks,"; \
			echo "  and pre-commit refuses to install while any hooksPath is set. Skipping hooks."; \
			echo "  To enable: 'git config --global --unset core.hooksPath', or chain the"; \
			echo "  pre-commit hooks from $$GLOBAL_HOOKS yourself. (make verify still gates.)"; \
		else \
			git config --local --unset-all core.hooksPath 2>/dev/null || true; \
			if uv run pre-commit install --install-hooks && uv run pre-commit install --hook-type pre-push; then \
				echo "Hooks installed: pre-commit, pre-push (.git/hooks)"; \
			else \
				echo "WARNING: pre-commit install failed — hooks NOT installed; run 'make verify' manually."; \
			fi; \
		fi; \
	fi

# ============================================================================
# Code Quality
# ============================================================================

lint:
	uv run ruff check screenscribe tests

format:
	uv run ruff format screenscribe tests
	uv run ruff check --fix screenscribe tests

# Non-mutating format gate (matches CI; release-check uses this, not `format`).
format-check:
	uv run ruff format --check screenscribe tests

# Verify the package actually builds (sdist + wheel) into ./dist.
build-check:
	uv build
	@printf '%s\n' 'Package builds (sdist + wheel).'

# Portable READY / NOT READY verifier (scripts/ss_verify.py). Cross-platform,
# fail-closed, effect-level. THE single source of truth for "does it work":
# runs the full check sequence against this folder (no-junk, no-secrets,
# leak-scan, branding, compiles, lint+format+types, security/bandit,
# semgrep, tests+coverage, buildable, cli+effect-smoke) and prints per-check
# ✔/✘ + RESULT: READY|NOT READY. The semgrep step runs the SAME ruleset
# (semgrep.yml) the pre-commit hook enforces, so the gate no longer reports
# READY while semgrep lives only in opt-in pre-commit. release-check and
# ship-verify are thin shims over this; CI calls it.
verify:
	$(PYTHON) scripts/ss_verify.py .

# Audit the SHIPPABLE tree the way the world receives it: git-archive HEAD into
# a temp dir (tracked files only, single git-archive path — no ZIP tooling) and
# run ss-verify on that extract. Proves the exported tree is READY, not just the
# working copy. This is the single seed-audit path now (the old ZIP-rooted
# build/audit seed scripts are gone — git-archive is the only seed flow).
#
# The extract is git-init'd into a fresh, single-commit history (exactly the
# "fresh history" a public seed starts with): git-archive strips the .git dir,
# but several checks AND repo tests legitimately use `git ls-files` against the
# tree, so the seed audit must present a real working tree. VIRTUAL_ENV is
# cleared so the nested `uv run` builds the seed's OWN env instead of inheriting
# this checkout's .venv.
verify-seed:
	@set -e; \
	printf '%s\n' 'Auditing shippable tree via git-archive HEAD -> ss-verify...'; \
	TMP=$$(mktemp -d); \
	trap 'rm -rf "$$TMP"' EXIT; \
	git archive --format=tar HEAD | tar -x -C "$$TMP"; \
	git -C "$$TMP" init -q -b main; \
	git -C "$$TMP" add -A; \
	git -C "$$TMP" -c user.email=seed@local -c user.name=seed commit -qm seed; \
	printf 'archived + fresh-history seed at %s\n' "$$TMP"; \
	env -u VIRTUAL_ENV $(PYTHON) scripts/ss_verify.py "$$TMP"

# Pre-ship gate, now a thin shim over ss-verify: ss-verify's cli+effect-smoke
# check installs the freshly built WHEEL into an isolated env OUTSIDE the source
# tree and proves the packaged artifact RENDERS a self-contained REVIEW report
# (embedded base64 image, no external <script src=>, no missing template).
# This absorbs the old standalone ship-verify (effect-level, fail-closed) so
# there is one source of truth, not two.
ship-verify: verify

# Full pre-release gate, now a thin shim over ss-verify. ss-verify covers at
# least everything this gate used to: lint + ruff format-check + mypy +
# security/bandit + tests + coverage floor + leak-scan + secrets baseline +
# build + ship-verify (effect-level wheel render). One gate, one truth.
release-check: verify
	@printf '%s\n' 'release-check delegates to ss-verify (make verify): the single READY gate.'

typecheck:
	uv run mypy screenscribe

security:
	uv run bandit -r screenscribe -c pyproject.toml

check: lint typecheck security
	@printf '%s\n' 'All quality checks passed!'

# Leak scan over the tracked tree. The PUBLIC scan is intentionally GENERIC: it
# looks for risk *classes* (private keys, provider tokens, local user paths) and
# forbidden artifact/media files — never project-private names. Operator-side
# project patterns, if any, live in .private/leak-patterns.txt (gitignored,
# never part of the public tree); the scan consumes that file only when present.
leak-scan:
	@git rev-parse --is-inside-work-tree >/dev/null 2>&1 || { printf '%s\n' 'FAIL: leak-scan requires a git working tree (git ls-files drives the scan).'; exit 1; }
	@printf '%s\n' 'Leak scan (tracked tree)...'
	@hits=$$(git ls-files -z | xargs -0 grep -lIE -e '-----BEGIN [A-Z ]*PRIVATE KEY-----|sk-[A-Za-z0-9_-]{20,}|ghp_[A-Za-z0-9]{30,}|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,}|/Users/[A-Za-z]|/home/[A-Za-z]' 2>/dev/null); if [ -n "$$hits" ]; then printf 'FAIL: secret / token / local-path class in:\n%s\n' "$$hits"; exit 1; else printf '%s\n' '  generic content scan clean'; fi
	@files=$$(git ls-files | grep -iE '\.(zip|tar|gz|tgz|mp4|mov|mkv|webm|avi|log|pem|key)$$' || true); if [ -n "$$files" ]; then printf 'FAIL: forbidden artifact/media files tracked:\n%s\n' "$$files"; exit 1; else printf '%s\n' '  artifact/media scan clean'; fi
	@if [ -f .private/leak-patterns.txt ]; then hits=$$(git ls-files -z | xargs -0 grep -lIiEf .private/leak-patterns.txt 2>/dev/null || true); if [ -n "$$hits" ]; then printf 'FAIL: project-private pattern in:\n%s\n' "$$hits"; exit 1; else printf '%s\n' '  operator-side private-pattern scan clean'; fi; else printf '%s\n' '  (no .private/leak-patterns.txt; operator-side scan skipped)'; fi

# Branding guard (WARIANT A): scan only the public-surface path list for bare
# camelCase ScreenScribe/VetCoders brand regressions. Allowlisted technical
# identifiers (ScreenScribeConfig, ScreenScribeLib, ScreenScribePlayer,
# X-ScreenScribe-Token, the SCREENSCRIBE_ env prefix) are exempt. Same check
# `make verify` runs in run_all; this is the standalone entry.
brand-scan:
	$(PYTHON) scripts/ss_verify.py . --branding-only

# Baseline-aware secrets scan (same engine as the pre-commit hook). Only the
# vendored minified JSZip is excluded (entropy false positive), matching
# .pre-commit-config.yaml / .screenscribe-verify.yml. site/demo is NOT
# excluded: a real secret pushed into the generated demo report must be caught
# (fail-closed); its benign assets are pinned per-file in .secrets.baseline.
# Lightweight by design. semgrep is now part of THE gate (`make verify` runs
# the semgrep.yml ruleset); this target stays focused on the detect-secrets
# baseline. The rest of the pre-commit suite can be run manually via
# `make precommit-check`.
secrets-check:
	@printf '%s\n' 'detect-secrets (baseline) ...'
	@git ls-files -z -- . ':(exclude)screenscribe/html_pro_assets/vendor' | xargs -0 uv run detect-secrets-hook --baseline .secrets.baseline && printf '%s\n' '  no new secrets vs baseline'

precommit-check:
	uv run pre-commit run --all-files

# ============================================================================
# Testing
# ============================================================================

# Default test target - unit tests only (fast, no API required)
test: test-unit

test-unit:
	uv run pytest tests/ -v -m "not integration" --tb=short

test-integration:
	uv run pytest tests/ -v -m "integration" --run-integration --tb=short

test-all:
	uv run pytest tests/ -v --run-integration --tb=short

test-cov:
	uv run pytest tests/ -v -m "not integration" --cov=screenscribe --cov-report=term-missing --cov-report=html

# Lean coverage gate for CI / release-check: enforces the fail_under floor
# from pyproject [tool.coverage.report]. No html, fast.
cov-check:
	uv run pytest tests/ -q -m "not integration" --cov=screenscribe --cov-report=term-missing

# End-to-end review suite: builds a wheel from HEAD, installs it into an isolated
# venv OUTSIDE the repo, generates a real report, and drives it in a headless
# Chromium via Playwright (token reload, manual-frame image survival across
# reload + cross-window storage sync, ZIP export). Deliberately NOT part of
# `make verify` (verify stays fast + browserless). Requires the `playwright`
# dev dep + a cached Chromium (no `playwright install` download needed locally).
e2e-review:
	uv run pytest tests/e2e -m e2e --run-e2e -v

# ============================================================================
# Development Helpers
# ============================================================================

run:
	uv run screenscribe --help

analyze:
	@if [ -z "$(VIDEO)" ]; then \
		echo "Usage: make analyze VIDEO=path/to/video.mov [PORT=8766]"; \
		exit 1; \
	fi
	uv run screenscribe analyze "$(VIDEO)" --port $(or $(PORT),8766)

clean:
	rm -rf .pytest_cache
	rm -rf .mypy_cache
	rm -rf .ruff_cache
	rm -rf htmlcov
	rm -rf .coverage
	rm -rf dist
	rm -rf build
	rm -rf *.egg-info
	rm -rf screenscribe/__pycache__
	rm -rf tests/__pycache__
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true

# CI targets
ci-lint: lint typecheck security

ci-test:
	uv run pytest tests/ -v -m "not integration" --tb=short --junitxml=test-results.xml

ci-test-integration:
	uv run pytest tests/ -v -m "integration" --run-integration --tb=short --junitxml=integration-results.xml

# ============================================================================
# Versioning
# ============================================================================

# Get current version from pyproject.toml
CURRENT_VERSION := $(shell grep '^version = ' pyproject.toml | sed 's/version = "\(.*\)"/\1/')

version:
	@printf '%s\n' 'Current version: $(CURRENT_VERSION)'

# Version bumps edit pyproject.toml + promote the CHANGELOG "Unreleased" section
# in one shot (scripts/bump_version.sh). They do NOT reinstall: __version__ reads
# installed package metadata, so run `make install` afterwards for
# `screenscribe --version` and `make verify` to reflect the new number.
version-patch:
	@printf '%s\n' 'Bumping patch version...'
	@scripts/bump_version.sh patch

version-minor:
	@printf '%s\n' 'Bumping minor version...'
	@scripts/bump_version.sh minor

version-major:
	@printf '%s\n' 'Bumping major version...'
	@scripts/bump_version.sh major

# -----------------------------------------------------------------------------
# Living Tree race-protected commit
# Wraps "stage named files + commit" with HEAD-shift + foreign-file race
# detection so concurrent agents on one Living Tree checkout cannot interleave
# one commit message under another tree. Recovery is operator-driven.
#   make commit-safe MSG="<subject>" FILES="p1 p2 ..."
#   make commit-safe MSG_FILE=<path> FILES="p1 p2 ..."   # multi-line body
#   make test-race-protection
# -----------------------------------------------------------------------------
commit-safe:
	@if [ -z "$(FILES)" ]; then \
		echo "usage:" >&2; \
		echo "  make commit-safe MSG=\"<subject>\" FILES=\"path1 path2 ...\"" >&2; \
		echo "  make commit-safe MSG_FILE=<path>  FILES=\"path1 path2 ...\"" >&2; \
		echo "" >&2; \
		echo "Race-protected commit helper for Living Tree workflow." >&2; \
		echo "MSG_FILE supports multi-line commit bodies (Plan 07-b)." >&2; \
		exit 1; \
	fi
	@if [ -n "$(MSG_FILE)" ] && [ -n "$(MSG)" ]; then \
		echo "make commit-safe: pass MSG OR MSG_FILE, not both" >&2; \
		exit 1; \
	fi
	@if [ -z "$(MSG)" ] && [ -z "$(MSG_FILE)" ]; then \
		echo "make commit-safe: MSG=\"...\" or MSG_FILE=<path> is required" >&2; \
		exit 1; \
	fi
	@if [ -n "$(MSG_FILE)" ]; then \
		bash scripts/living-tree-commit.sh --message-file "$(MSG_FILE)" -- $(FILES); \
	else \
		bash scripts/living-tree-commit.sh "$(MSG)" -- $(FILES); \
	fi

test-race-protection:
	@bash tests/race_protection_test.sh
