"""CLI ergonomics: unhappy paths end in clean messages, not raw tracebacks.

Covers the four divergent error paths unified in PKG7/C7.3:
- FFmpeg/FFprobe missing guard, consistent across review/transcribe/preprocess
- ``analyze`` port fallback when the requested port is busy
- interactive-mode TTY guard (no EOFError on non-interactive stdin)
- ``config`` with no flag prints real command help, not an ad-hoc one-liner
"""

from __future__ import annotations

import socket
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from screenscribe.audio import FFmpegNotFoundError
from screenscribe.cli import app
from screenscribe.config import ScreenScribeConfig

FFMPEG_MISSING_MESSAGE = "Required tools not found: ffmpeg, ffprobe"


def _make_video(tmp_path: Path) -> Path:
    video = tmp_path / "sample.mov"
    video.write_bytes(b"fake-video")
    return video


def _raise_ffmpeg_missing() -> None:
    raise FFmpegNotFoundError(FFMPEG_MISSING_MESSAGE)


def _error_line(output: str) -> str:
    for line in output.splitlines():
        if "Error:" in line and FFMPEG_MISSING_MESSAGE in line:
            return line.strip()
    return ""


# ---------------------------------------------------------------------------
# FFmpeg guard
# ---------------------------------------------------------------------------


def test_preprocess_ffmpeg_missing_is_clean(monkeypatch: Any, tmp_path: Path) -> None:
    video = _make_video(tmp_path)
    monkeypatch.setattr("screenscribe.cli.check_ffmpeg_installed", _raise_ffmpeg_missing)

    result = CliRunner().invoke(app, ["preprocess", str(video)])

    assert result.exit_code == 1, result.output
    assert "Traceback" not in result.output
    assert "FFmpegNotFoundError" not in result.output
    assert FFMPEG_MISSING_MESSAGE in result.output


def test_transcribe_ffmpeg_missing_is_clean(monkeypatch: Any, tmp_path: Path) -> None:
    video = _make_video(tmp_path)
    monkeypatch.setattr(
        "screenscribe.cli.ScreenScribeConfig.load",
        lambda: ScreenScribeConfig(language="en"),
    )
    monkeypatch.setattr("screenscribe.cli.check_ffmpeg_installed", _raise_ffmpeg_missing)

    result = CliRunner().invoke(app, ["transcribe", str(video)])

    assert result.exit_code == 1, result.output
    assert "Traceback" not in result.output
    assert "FFmpegNotFoundError" not in result.output
    assert FFMPEG_MISSING_MESSAGE in result.output


@pytest.mark.parametrize("command", ["review", "transcribe", "preprocess"])
def test_ffmpeg_message_consistent_across_commands(
    monkeypatch: Any, tmp_path: Path, command: str
) -> None:
    """The ffmpeg-missing error is byte-identical across all three commands."""
    video = _make_video(tmp_path)
    monkeypatch.setattr(
        "screenscribe.cli.ScreenScribeConfig.load",
        lambda: ScreenScribeConfig(language="en"),
    )
    monkeypatch.setattr("screenscribe.cli.check_ffmpeg_installed", _raise_ffmpeg_missing)

    result = CliRunner().invoke(app, [command, str(video)])

    assert result.exit_code == 1, result.output
    assert "Traceback" not in result.output
    # The shared guard renders the same "Error: ..." line regardless of command.
    assert _error_line(result.output) == f"Error: {FFMPEG_MISSING_MESSAGE}"


# ---------------------------------------------------------------------------
# analyze port fallback
# ---------------------------------------------------------------------------


def _stub_analyze(monkeypatch: Any, recorded: dict[str, Any]) -> None:
    class _FakeApp:
        class state:
            session_token = "t"  # noqa: S105 - test fixture, not a credential

    monkeypatch.setattr(
        "screenscribe.cli.ScreenScribeConfig.load",
        lambda: ScreenScribeConfig(language="en", vision_api_key="test-key"),
    )
    monkeypatch.setattr(
        "screenscribe.analyze_server.create_analyze_app",
        lambda _video, _config: _FakeApp(),
    )
    monkeypatch.setattr(
        "screenscribe.cli.webbrowser", type("W", (), {"open": staticmethod(lambda _u: None)})
    )
    # analyze now runs a model-availability pre-flight; stub it so the port tests
    # never touch the network.
    monkeypatch.setattr("screenscribe.cli.validate_models", lambda *_a, **_k: None)

    def _sniff(*_args: Any, **kwargs: Any) -> None:
        recorded["port"] = kwargs.get("port")

    monkeypatch.setattr("uvicorn.run", _sniff)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_analyze_port_fallback_when_busy(monkeypatch: Any, tmp_path: Path) -> None:
    video = _make_video(tmp_path)
    recorded: dict[str, Any] = {}
    _stub_analyze(monkeypatch, recorded)

    busy = _free_port()
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind(("127.0.0.1", busy))
    try:
        result = CliRunner().invoke(app, ["analyze", str(video), "--port", str(busy)])
    finally:
        holder.close()

    assert result.exit_code == 0, result.output
    assert "Traceback" not in result.output
    assert recorded["port"] != busy
    # URL/panel must advertise the SAME resolved port uvicorn actually binds.
    assert f"localhost:{recorded['port']}" in result.output


def test_analyze_port_default_when_free(monkeypatch: Any, tmp_path: Path) -> None:
    video = _make_video(tmp_path)
    recorded: dict[str, Any] = {}
    _stub_analyze(monkeypatch, recorded)

    free = _free_port()
    result = CliRunner().invoke(app, ["analyze", str(video), "--port", str(free)])

    assert result.exit_code == 0, result.output
    assert recorded["port"] == free


# ---------------------------------------------------------------------------
# interactive TTY guard
# ---------------------------------------------------------------------------


def test_interactive_tty_guard_no_eoferror() -> None:
    """No subcommand on non-interactive stdin -> clean help, never EOFError."""
    result = CliRunner().invoke(app, [])

    assert "Traceback" not in result.output
    assert "EOFError" not in result.output
    assert result.exit_code == 2
    assert "Interactive mode requires a terminal" in result.output


# ---------------------------------------------------------------------------
# config no-flag help
# ---------------------------------------------------------------------------


def test_config_no_flag_shows_real_help() -> None:
    result = CliRunner().invoke(app, ["config"])

    assert result.exit_code == 0, result.output
    assert "Usage" in result.output
    assert "--show" in result.output
