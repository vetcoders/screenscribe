# Contributing to screenscribe

Thank you for your interest in contributing to screenscribe! We welcome contributions from the community to help make video review automation better for everyone.

## Development Setup

We use `uv` for dependency management and Python environment handling.

1.  **Clone the repository:**
    ```bash
    git clone https://github.com/vetcoders/screenscribe.git
    cd screenscribe
    ```

2.  **Install dependencies (with dev tools):**
    ```bash
    uv sync --dev
    ```

3.  **Run the CLI:**
    ```bash
    uv run screenscribe --help
    ```

## Coding Standards

- **Formatting**: We use `ruff` for formatting and linting. Run `make format` to format your code.
- **Type Hints**: All code must have strict type hints. We use `mypy` for type checking. Run `make typecheck`.
- **Linting**: Run `make lint` to check for code quality issues.
- **Tests**: We use `pytest`. Run `make test` for unit tests. Integration tests (`make test-integration`) require a `LIBRAXIS_API_KEY`. There is also an optional end-to-end suite (`make e2e-review`) that drives a real report in headless Chromium via Playwright — it needs the `playwright` dev dependency and a cached Chromium, and is deliberately kept out of `make verify` so the gate stays fast and browserless.
- **Architecture**: New to the codebase? [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) maps the pipeline, servers, report layer, CLI, and the hub modules to respect before editing.

## Pull Request Process

1.  Create a new branch for your feature or bugfix: `git checkout -b feat/your-feature-name`.
2.  Run the one gate before pushing: **`make verify`**. This is ss-verify
    (`scripts/ss_verify.py`) — the single source of truth for "does it work". It
    runs the full sequence against your checkout (no-junk · no-secrets ·
    leak-scan · compiles · ruff check + ruff format --check + mypy · bandit
    security · pytest + coverage floor · build · isolated-wheel REVIEW render)
    and prints `RESULT: READY` / `RESULT: NOT READY`. CI runs the same gate, so
    green locally means green CI. The individual targets (`make lint`,
    `make typecheck`, `make security`, `make test`) remain available for fast
    iteration, but `make verify` is what must pass.
3.  Do not commit secrets, internal/private data, or large media/binaries. CI and
    the pre-commit hooks run `detect-secrets`; keep API keys in `.env` (gitignored).
    `make verify` includes the leak scan (secret/token/local-path classes and
    forbidden artifact/media files); run `make verify-seed` to audit the exact
    shippable tree (`git archive HEAD` → ss-verify) before a public release.
4.  Commit using [Conventional Commits](https://www.conventionalcommits.org/) where possible.
5.  Push your branch and open a Pull Request against `main`. The PR template
    includes a checklist; CI must pass before merge.

## Code of Conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md). By
participating, you are expected to uphold it. Report unacceptable behavior to
**conduct@vetcoders.io**.

## Security

Found a vulnerability? Please **do not** open a public issue — see
[SECURITY.md](SECURITY.md) for private reporting.
