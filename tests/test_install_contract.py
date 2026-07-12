from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MAKE = shutil.which("make")
MAKE_COMMAND = MAKE or "make"
pytestmark = pytest.mark.skipif(MAKE is None, reason="make is not available")


def test_user_install_excludes_contributor_bootstrap_and_reports_progress() -> None:
    result = subprocess.run(
        [MAKE_COMMAND, "--dry-run", "install"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    output = result.stdout
    assert "[1/3]" in output
    assert "[2/3]" in output
    assert "[3/3]" in output
    assert "command -v uv" in output
    assert 'PATH="$(uv tool dir --bin):$PATH"' in output
    assert "uv tool install . --reinstall --force" in output
    assert "uv tool uninstall screenscribe" not in output
    assert "uv tool update-shell" in output
    assert "pre-commit install" not in output
    assert "uv sync --dev" not in output


def test_contributor_install_keeps_hooks_and_dev_dependencies() -> None:
    result = subprocess.run(
        [MAKE_COMMAND, "--dry-run", "dev"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    output = result.stdout
    assert "pre-commit install" in output
    assert "uv sync --dev" in output


def test_public_ffmpeg_guidance_covers_shared_macos_without_chown() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    site = (ROOT / "site" / "index.html").read_text(encoding="utf-8")

    for public_surface in (readme, site):
        normalized = " ".join(public_surface.lower().split())
        assert "Homebrew owner or an administrator" in public_surface
        assert "do not change ownership of the homebrew prefix" in normalized
        assert "chown" not in public_surface
