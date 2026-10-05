"""Preset validation must finish before model preflight or paid review work."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from screenscribe import cli, review_pipeline
from screenscribe.config import ScreenScribeConfig


@pytest.mark.parametrize(
    ("case", "expected_message"),
    [
        ("missing-file", "--preset custom requires"),
        ("unsafe-category", "Unsafe category name"),
    ],
)
def test_invalid_custom_preset_stops_before_model_preflight_and_pipeline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    expected_message: str,
) -> None:
    video = tmp_path / "clip.mov"
    video.write_bytes(b"video")
    args = [
        "review",
        str(video),
        "--preset",
        "custom",
        "--no-audio",
        "--no-serve",
    ]
    if case == "unsafe-category":
        keywords_file = tmp_path / "unsafe.yaml"
        keywords_file.write_text("../../../outside:\n  - phrase\n", encoding="utf-8")
        args.extend(["--keywords-file", str(keywords_file)])

    forbidden_calls: list[str] = []

    def forbid(name: str):
        def _forbidden(*args: Any, **kwargs: Any) -> None:
            forbidden_calls.append(name)
            raise AssertionError(f"{name} must not run before preset validation")

        return _forbidden

    monkeypatch.setattr(cli, "_check_ffmpeg_or_exit", lambda: None)
    monkeypatch.setattr(
        ScreenScribeConfig,
        "load",
        classmethod(lambda cls: ScreenScribeConfig(api_key="test-key")),  # pragma: allowlist secret
    )
    monkeypatch.setattr(cli, "_check_provider_config_or_exit", forbid("provider preflight"))
    monkeypatch.setattr(cli, "validate_models", forbid("model validation"))
    monkeypatch.setattr(review_pipeline, "run_review", forbid("paid review pipeline"))

    result = CliRunner().invoke(cli.app, args)

    assert result.exit_code == 1
    assert expected_message in result.output
    assert forbidden_calls == []
