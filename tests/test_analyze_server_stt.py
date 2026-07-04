from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from screenscribe.analyze_server import create_analyze_app
from screenscribe.config import ScreenScribeConfig
from screenscribe.transcribe import Segment, TranscriptionResult

VALID_BROWSER_AUDIO = b"voice-data" + (b"x" * 2048)


@pytest.fixture
def sample_video(tmp_path: Path) -> Path:
    video_path = tmp_path / "test_video.mp4"
    video_path.write_bytes(b"\x00\x00\x00\x14ftypmp42\x00\x00\x00\x00mp42")
    return video_path


def _config() -> ScreenScribeConfig:
    return ScreenScribeConfig(
        **{"api" + "_key": "test-key"},
        **{"vision_api" + "_key": "test-key"},
        stt_endpoint="https://api.example.com/v1/audio/transcriptions",
        stt_model="test-model",
    )


def test_analyze_stt_uses_direct_browser_upload_path(
    monkeypatch: pytest.MonkeyPatch, sample_video: Path
) -> None:
    def fake_transcribe_audio_bytes(*args: Any, **kwargs: Any) -> TranscriptionResult:
        assert args[0] == VALID_BROWSER_AUDIO
        assert args[1] == "recording.webm"
        assert kwargs["content_type"] == "audio/webm"
        return TranscriptionResult(
            text="Direct transcript",
            segments=[Segment(id=1, start=0.0, end=1.5, text="Direct transcript")],
            language="pl",
            response_id="resp_direct",
        )

    def fail_transcode(*args: Any, **kwargs: Any) -> Path:
        raise AssertionError("Normalization fallback should not run when direct upload succeeds")

    def fail_path_transcribe(*args: Any, **kwargs: Any) -> TranscriptionResult:
        raise AssertionError("Path-based transcription should not run when direct upload succeeds")

    monkeypatch.setattr(
        "screenscribe.transcribe.transcribe_audio_bytes", fake_transcribe_audio_bytes
    )
    monkeypatch.setattr("screenscribe.audio.normalize_audio_for_stt", fail_transcode)
    monkeypatch.setattr("screenscribe.transcribe.transcribe_audio", fail_path_transcribe)

    app = create_analyze_app(sample_video, _config())
    client = TestClient(app)

    response = client.post(
        "/api/stt",
        files={"audio": ("recording.webm", VALID_BROWSER_AUDIO, "audio/webm")},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["text"] == "Direct transcript"
    assert payload["response_id"] == "resp_direct"
    assert payload["segments"][0]["text"] == "Direct transcript"


def test_analyze_stt_runs_transcription_off_the_event_loop(
    monkeypatch: pytest.MonkeyPatch, sample_video: Path
) -> None:
    """FW-01: the blocking browser STT call (network round-trip plus a possible
    ffmpeg-normalization fallback) must be offloaded to the threadpool, not run
    directly on the async handler's event-loop thread. Executed on the loop
    thread, ``asyncio.get_running_loop()`` succeeds; offloaded to a worker
    thread, it raises ``RuntimeError`` (no running loop). We assert the latter.
    """
    observed: dict[str, bool] = {}

    def fake_transcribe_audio_bytes(*args: Any, **kwargs: Any) -> TranscriptionResult:
        try:
            asyncio.get_running_loop()
            observed["on_event_loop"] = True
        except RuntimeError:
            observed["on_event_loop"] = False
        return TranscriptionResult(
            text="Off-loop transcript",
            segments=[Segment(id=1, start=0.0, end=1.0, text="Off-loop transcript")],
            language="pl",
            response_id="resp_offloop",
        )

    monkeypatch.setattr(
        "screenscribe.transcribe.transcribe_audio_bytes", fake_transcribe_audio_bytes
    )

    app = create_analyze_app(sample_video, _config())
    client = TestClient(app)

    response = client.post(
        "/api/stt",
        files={"audio": ("recording.webm", VALID_BROWSER_AUDIO, "audio/webm")},
    )

    assert response.status_code == 200
    assert observed["on_event_loop"] is False


def test_analyze_stt_uses_config_language_as_spoken_language(
    monkeypatch: pytest.MonkeyPatch, sample_video: Path
) -> None:
    captured: dict[str, Any] = {}

    def fake_transcribe_audio_bytes(*args: Any, **kwargs: Any) -> TranscriptionResult:
        captured.update(kwargs)
        return TranscriptionResult(
            text="Polska notatka",
            segments=[Segment(id=1, start=0.0, end=1.5, text="Polska notatka")],
            language="pl",
            response_id="resp_pl",
        )

    monkeypatch.setattr(
        "screenscribe.transcribe.transcribe_audio_bytes", fake_transcribe_audio_bytes
    )

    config = _config()
    config.language = "pl"
    app = create_analyze_app(sample_video, config)
    client = TestClient(app)

    response = client.post(
        "/api/stt",
        files={"audio": ("recording.webm", VALID_BROWSER_AUDIO, "audio/webm")},
    )

    assert response.status_code == 200
    assert captured["language"] == "pl"


def test_analyze_stt_rejects_likely_silent_hallucination(
    monkeypatch: pytest.MonkeyPatch, sample_video: Path
) -> None:
    def fake_transcribe_audio_bytes(*args: Any, **kwargs: Any) -> TranscriptionResult:
        return TranscriptionResult(
            text="Thank you for watching.",
            segments=[
                Segment(
                    id=1,
                    start=0.0,
                    end=1.0,
                    text="Thank you for watching.",
                    no_speech_prob=0.92,
                )
            ],
            language="en",
            response_id="resp_hallucinated",
        )

    monkeypatch.setattr(
        "screenscribe.transcribe.transcribe_audio_bytes", fake_transcribe_audio_bytes
    )

    app = create_analyze_app(sample_video, _config())
    client = TestClient(app)

    response = client.post(
        "/api/stt",
        files={"audio": ("recording.webm", VALID_BROWSER_AUDIO, "audio/webm")},
    )

    assert response.status_code == 422
    assert "likely silent audio" in response.json()["detail"].lower()


def test_analyze_stt_falls_back_to_normalized_mp3(
    monkeypatch: pytest.MonkeyPatch, sample_video: Path, tmp_path: Path
) -> None:
    normalized_mp3 = tmp_path / "normalized.mp3"
    normalized_mp3.write_bytes(b"mp3-data")

    def fail_direct(*args: Any, **kwargs: Any) -> TranscriptionResult:
        raise RuntimeError("provider down")

    def fake_normalize(*args: Any, **kwargs: Any) -> Path:
        return normalized_mp3

    def fake_path_transcribe(*args: Any, **kwargs: Any) -> TranscriptionResult:
        assert args[0] == normalized_mp3
        return TranscriptionResult(
            text="Fallback transcript",
            segments=[Segment(id=1, start=0.0, end=2.0, text="Fallback transcript")],
            language="pl",
            response_id="resp_fallback",
        )

    monkeypatch.setattr("screenscribe.transcribe.transcribe_audio_bytes", fail_direct)
    monkeypatch.setattr("screenscribe.audio.normalize_audio_for_stt", fake_normalize)
    monkeypatch.setattr("screenscribe.transcribe.transcribe_audio", fake_path_transcribe)

    app = create_analyze_app(sample_video, _config())
    client = TestClient(app)

    response = client.post(
        "/api/stt",
        files={"audio": ("recording.webm", VALID_BROWSER_AUDIO, "audio/webm")},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["text"] == "Fallback transcript"
    assert payload["response_id"] == "resp_fallback"


def test_analyze_stt_returns_502_on_transcription_failure(
    monkeypatch: pytest.MonkeyPatch, sample_video: Path
) -> None:
    def fail_direct(*args: Any, **kwargs: Any) -> TranscriptionResult:
        raise RuntimeError("provider down")

    def fail_normalize(*args: Any, **kwargs: Any) -> Path:
        raise RuntimeError("ffmpeg unavailable")

    monkeypatch.setattr("screenscribe.transcribe.transcribe_audio_bytes", fail_direct)
    monkeypatch.setattr("screenscribe.audio.normalize_audio_for_stt", fail_normalize)

    app = create_analyze_app(sample_video, _config())
    client = TestClient(app)

    response = client.post(
        "/api/stt",
        files={"audio": ("recording.webm", VALID_BROWSER_AUDIO, "audio/webm")},
    )

    assert response.status_code == 502
    assert response.json()["detail"] == "Voice transcription failed. Try again in a moment."


def test_analyze_stt_rejects_empty_upload(sample_video: Path) -> None:
    app = create_analyze_app(sample_video, _config())
    client = TestClient(app)

    response = client.post(
        "/api/stt",
        files={"audio": ("recording.webm", b"", "audio/webm")},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Voice recording is empty."


def test_analyze_stt_rejects_tiny_browser_recording_before_provider(
    monkeypatch: pytest.MonkeyPatch, sample_video: Path
) -> None:
    def fail_transcribe(*args: Any, **kwargs: Any) -> TranscriptionResult:
        raise AssertionError("Tiny browser recordings should not reach STT provider")

    monkeypatch.setattr("screenscribe.transcribe.transcribe_audio_bytes", fail_transcribe)

    app = create_analyze_app(sample_video, _config())
    client = TestClient(app)

    response = client.post(
        "/api/stt",
        files={"audio": ("recording.webm", b"\x1aE\xdf\xa3", "audio/webm")},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Voice recording is too short. Hold to record longer."


def test_analyze_stt_rejects_oversized_upload(sample_video: Path) -> None:
    """A browser STT upload over the cap is rejected with 413 before any
    transcription / network call (mirrors the review server's limit)."""
    app = create_analyze_app(sample_video, _config())
    client = TestClient(app)
    oversized = b"x" * (25 * 1024 * 1024 + 1)
    response = client.post(
        "/api/stt",
        files={"audio": ("big.webm", oversized, "audio/webm")},
    )
    assert response.status_code == 413
    assert response.json()["detail"] == "Audio upload exceeds 25 MB limit."
