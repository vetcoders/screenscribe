"""C5.3 — the analyze session temp dir is reclaimed on shutdown.

``AnalyzeSession.frames_dir`` is a ``tempfile.mkdtemp`` directory that used to
be left to OS-level cleanup. ``create_analyze_app`` now exposes the session on
``app.state.session`` and registers a FastAPI ``shutdown`` handler plus an
``atexit`` fallback that ``rmtree`` the directory deterministically.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

import screenscribe.analyze_server as analyze_server
from screenscribe.analyze_server import create_analyze_app
from screenscribe.config import ScreenScribeConfig


def _sample_video(tmp_path: Path) -> Path:
    video_path = tmp_path / "test_video.mp4"
    video_path.write_bytes(b"\x00\x00\x00\x14ftypmp42\x00\x00\x00\x00mp42")
    return video_path


def _config() -> ScreenScribeConfig:
    return ScreenScribeConfig(
        api_key="test-key",  # pragma: allowlist secret
        vision_api_key="test-key",  # pragma: allowlist secret
        stt_endpoint="https://api.example.com/v1/audio/transcriptions",
        stt_model="test-model",
    )


def test_session_exposed_with_existing_frames_dir(tmp_path: Path) -> None:
    """A2: app.state.session.frames_dir is a live directory right after create."""
    app = create_analyze_app(_sample_video(tmp_path), _config())
    session = app.state.session
    assert isinstance(session.frames_dir, Path)
    assert session.frames_dir.exists()
    assert session.frames_dir.is_dir()


def test_shutdown_handler_removes_frames_dir(tmp_path: Path) -> None:
    """A3: FastAPI shutdown (TestClient context exit) removes frames_dir."""
    app = create_analyze_app(_sample_video(tmp_path), _config())
    frames_dir = app.state.session.frames_dir
    assert frames_dir.exists()

    with TestClient(app):
        # Still present while the server is up.
        assert frames_dir.exists()

    # Graceful shutdown fired -> directory gone.
    assert not frames_dir.exists()


def test_atexit_fallback_registered_and_cleans(tmp_path: Path, monkeypatch) -> None:
    """A4/A5: an atexit fallback is registered, removes the dir, and is idempotent."""
    captured: list = []

    def _capture(fn):  # type: ignore[no-untyped-def]
        captured.append(fn)
        return fn

    monkeypatch.setattr(analyze_server.atexit, "register", _capture)

    app = create_analyze_app(_sample_video(tmp_path), _config())
    frames_dir = app.state.session.frames_dir
    assert frames_dir.exists()
    assert captured, "no atexit cleanup callback was registered"

    cleanup = captured[-1]
    cleanup()
    assert not frames_dir.exists()

    # A5: a second call must not raise and must not recreate the directory.
    cleanup()
    assert not frames_dir.exists()
