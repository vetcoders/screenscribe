"""Release metadata regression tests."""

import tomllib
from pathlib import Path
from typing import Any

from screenscribe import __version__
from screenscribe.analyze_server import create_analyze_app
from screenscribe.config import ScreenScribeConfig
from screenscribe.review_server import create_review_app


def _read_pyproject() -> dict[str, Any]:
    repo_root = Path(__file__).resolve().parents[1]
    return tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))


def test_package_version_matches_project_metadata() -> None:
    pyproject = _read_pyproject()
    assert pyproject["project"]["version"] == __version__


def test_current_version_has_changelog_entry() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    changelog = (repo_root / "CHANGELOG.md").read_text(encoding="utf-8")
    # Accept a heading for the current version in either Keep-a-Changelog
    # (`## [x.y.z]`) or the plain release form (`## x.y.z — ...`).
    assert f"## [{__version__}]" in changelog or f"## {__version__}" in changelog


def _server_config() -> ScreenScribeConfig:
    return ScreenScribeConfig(
        api_key="test-key",  # pragma: allowlist secret
        vision_api_key="test-key",  # pragma: allowlist secret
        stt_endpoint="https://api.example.com/v1/audio/transcriptions",
        stt_model="test-model",
    )


def test_review_server_version_matches_package_metadata(tmp_path: Path) -> None:
    output_dir = tmp_path / "review"
    output_dir.mkdir()
    report_file = output_dir / "sample_report.html"
    report_file.write_text("<html><body>report</body></html>", encoding="utf-8")
    video_path = output_dir / "sample.mov"
    video_path.write_bytes(b"\x00\x00\x00\x14ftypmp42\x00\x00\x00\x00mp42")

    app = create_review_app(output_dir, report_file.name, video_path, _server_config())

    assert app.version == __version__


def test_analyze_server_version_matches_package_metadata(tmp_path: Path) -> None:
    video_path = tmp_path / "sample.mov"
    video_path.write_bytes(b"\x00\x00\x00\x14ftypmp42\x00\x00\x00\x00mp42")

    app = create_analyze_app(video_path, _server_config())

    assert app.version == __version__
