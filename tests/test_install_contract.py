from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAKE = shutil.which("make")


def test_user_install_excludes_contributor_bootstrap_and_reports_progress() -> None:
    assert MAKE is not None
    result = subprocess.run(
        [MAKE, "--dry-run", "install"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    output = result.stdout
    assert "[1/3]" in output
    assert "[2/3]" in output
    assert "[3/3]" in output
    assert "uv tool install . --reinstall --force" in output
    assert "pre-commit install" not in output
    assert "uv sync --dev" not in output


def test_contributor_install_keeps_hooks_and_dev_dependencies() -> None:
    assert MAKE is not None
    result = subprocess.run(
        [MAKE, "--dry-run", "dev"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    output = result.stdout
    assert "pre-commit install" in output
    assert "uv sync --dev" in output
