"""``screenscribe serve`` starts the review server for an existing output directory.

The server itself is ``_serve_report`` (the ``review --serve`` path). These tests
pin the new command: help, app wiring on 127.0.0.1, a clear refusal for a bad
directory, and the auto-review skip so ``serve`` is not rewritten into ``review``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from typer.testing import CliRunner

import screenscribe.cli as cli
import screenscribe.review_server as review_server
from screenscribe.cli import _auto_review_if_video, app

_VIDEO_BYTES = b"\x00\x00\x00\x14ftypmp42\x00\x00\x00\x00mp42"


class _FakeAppState:
    session_token = "tok"  # noqa: S105 - test fixture, not a credential


class _FakeApp:
    state = _FakeAppState()


def _review_dir(tmp_path: Path, *, video_inside: bool = True) -> tuple[Path, Path]:
    """Minimal review bundle: report html + report.json + dummy video bytes."""
    output_dir = tmp_path / "screen_review"
    output_dir.mkdir()
    (output_dir / "screen_report.html").write_text(
        "<html><body>report</body></html>",
        encoding="utf-8",
    )
    (output_dir / "screen_report.json").write_text(
        json.dumps({"video": "screen.mov", "findings": []}),
        encoding="utf-8",
    )
    video = (output_dir if video_inside else tmp_path) / "screen.mov"
    video.write_bytes(_VIDEO_BYTES)
    return output_dir, video


def _invoke_serve(
    monkeypatch: pytest.MonkeyPatch, args: list[str]
) -> tuple[Any, dict[str, Any], dict[str, Any]]:
    created: dict[str, Any] = {}
    served: dict[str, Any] = {}

    def fake_create(**kwargs: Any) -> _FakeApp:
        created.update(kwargs)
        return _FakeApp()

    def fake_run(_app: Any, **kwargs: Any) -> None:
        served["host"] = kwargs.get("host")
        served["port"] = kwargs.get("port")

    monkeypatch.setattr(review_server, "create_review_app", fake_create)
    monkeypatch.setattr(uvicorn, "run", fake_run)
    monkeypatch.setattr(cli.webbrowser, "open", lambda _url: None)
    result = CliRunner().invoke(app, args)
    return result, created, served


def test_serve_help_exits_zero_and_names_the_command() -> None:
    result = CliRunner().invoke(app, ["serve", "--help"])
    assert result.exit_code == 0
    assert result.exception is None
    assert "serve" in result.output


def test_serve_existing_dir_builds_review_app_on_localhost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_dir, video = _review_dir(tmp_path, video_inside=True)
    result, created, served = _invoke_serve(
        monkeypatch, ["serve", str(output_dir), "--port", "9123"]
    )

    assert result.exit_code == 0, result.output
    assert result.exception is None
    assert created["output_dir"] == output_dir
    assert created["report_filename"] == "screen_report.html"
    assert created["video_path"] == video
    assert served["host"] == "127.0.0.1"
    # The in-directory recording is a real file, not the temporary symlink.
    assert video.is_file()
    assert not video.is_symlink()


def test_serve_keeps_a_video_symlink_already_in_the_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A recording the operator already linked into the review dir must survive."""
    output_dir = tmp_path / "screen_review"
    output_dir.mkdir()
    real = tmp_path / "real.mov"
    real.write_bytes(_VIDEO_BYTES)
    link = output_dir / "screen.mov"
    link.symlink_to(real)
    (output_dir / "screen_report.html").write_text("<html></html>", encoding="utf-8")
    (output_dir / "screen_report.json").write_text(
        json.dumps({"video": "screen.mov", "findings": []}),
        encoding="utf-8",
    )

    result, created, served = _invoke_serve(monkeypatch, ["serve", str(output_dir)])

    assert result.exit_code == 0, result.output
    assert created["video_path"] == link
    assert created["report_filename"] == "screen_report.html"
    assert served["host"] == "127.0.0.1"
    assert link.is_symlink()
    assert link.resolve() == real.resolve()


def test_serve_explicit_video_outside_the_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_dir, video = _review_dir(tmp_path, video_inside=False)
    result, created, served = _invoke_serve(
        monkeypatch,
        ["serve", str(output_dir), "--video", str(video)],
    )

    assert result.exit_code == 0, result.output
    assert created["output_dir"] == output_dir
    assert created["report_filename"] == "screen_report.html"
    assert created["video_path"] == video
    assert served["host"] == "127.0.0.1"
    # review --serve symlinks the outside video for the session, then removes it.
    assert not (output_dir / video.name).exists()


@pytest.mark.parametrize(
    ("setup", "needle"),
    [
        ("missing", "Review directory not found"),
        ("empty", "No review report"),
        ("file", "Not a directory"),
        ("html-only", "no matching source video"),
    ],
)
def test_serve_rejects_missing_or_invalid_dir_without_traceback(
    tmp_path: Path, setup: str, needle: str
) -> None:
    if setup == "missing":
        target = tmp_path / "does-not-exist"
    elif setup == "empty":
        target = tmp_path / "empty"
        target.mkdir()
    elif setup == "file":
        target = tmp_path / "not-a-dir"
        target.write_text("nope", encoding="utf-8")
    else:
        target = tmp_path / "html-only"
        target.mkdir()
        (target / "screen_report.html").write_text("<html></html>", encoding="utf-8")
        (target / "screen_report.json").write_text("{}", encoding="utf-8")

    result = CliRunner().invoke(app, ["serve", str(target)])

    # typer.Exit(1) surfaces as SystemExit. That is the clean refusal; a real
    # bug would be an unrelated exception or a traceback in the output.
    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert result.exception.code == 1
    assert "Traceback" not in result.output
    assert needle in result.output


def test_auto_review_does_not_treat_serve_as_a_video(monkeypatch: pytest.MonkeyPatch) -> None:
    argv = ["screenscribe", "serve", "clip.mov"]
    monkeypatch.setattr(sys, "argv", argv)
    _auto_review_if_video()
    assert argv == ["screenscribe", "serve", "clip.mov"]


def test_auto_review_still_injects_review_for_a_video(monkeypatch: pytest.MonkeyPatch) -> None:
    argv = ["screenscribe", "clip.mov"]
    monkeypatch.setattr(sys, "argv", argv)
    _auto_review_if_video()
    assert argv == ["screenscribe", "review", "clip.mov"]
