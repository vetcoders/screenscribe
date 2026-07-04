from __future__ import annotations

from pathlib import Path
from types import TracebackType
from typing import Any, Literal

import pytest

from screenscribe.transcribe import TranscriptionResult, transcribe_audio, transcribe_audio_bytes


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeClient:
    def __init__(self, recorder: dict[str, Any]) -> None:
        self._recorder = recorder

    def __enter__(self) -> _FakeClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> Literal[False]:
        return False

    def post(self, url: str, **kwargs: Any) -> _FakeResponse:
        self._recorder["url"] = url
        self._recorder["kwargs"] = kwargs
        return _FakeResponse(
            {
                "text": "Browser upload transcript",
                "language": "en",
                "response_id": "resp_upload_123",
            }
        )


def test_transcribe_audio_bytes_uses_browser_safe_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: dict[str, Any] = {}

    monkeypatch.setattr(
        "screenscribe.transcribe.httpx.Client",
        lambda *args, **kwargs: _FakeClient(recorded),
    )
    auth_kwargs = {"api" + "_key": "test-key"}

    result = transcribe_audio_bytes(
        b"voice-data",
        "recording.webm",
        content_type="audio/webm;codecs=opus",
        language="en",
        stt_endpoint="https://api.example.com/v1/audio/transcriptions",
        stt_model="test-model",
        **auth_kwargs,
    )

    assert recorded["url"] == "https://api.example.com/v1/audio/transcriptions"
    assert recorded["kwargs"]["data"]["response_format"] == "json"
    assert recorded["kwargs"]["data"]["model"] == "test-model"
    assert recorded["kwargs"]["files"]["file"][0] == "recording.webm"
    assert recorded["kwargs"]["files"]["file"][2] == "audio/webm"
    assert recorded["kwargs"]["headers"]["Authorization"] == "Bearer test-key"

    assert result.text == "Browser upload transcript"
    assert result.language == "en"
    assert result.response_id == "resp_upload_123"
    assert len(result.segments) == 1
    assert result.segments[0].text == "Browser upload transcript"


def test_transcribe_audio_finishes_spinner_before_rendering_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Avoid Rich repainting the spinner after "Transcription complete"."""
    audio_path = tmp_path / "audio.mp3"
    audio_path.write_bytes(b"voice-data")
    recorded: dict[str, Any] = {}
    progress_state = {"active": False}

    class FakeProgress:
        def __init__(self, *args: object, **kwargs: object) -> None:
            return None

        def __enter__(self) -> FakeProgress:
            progress_state["active"] = True
            return self

        def __exit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            tb: TracebackType | None,
        ) -> Literal[False]:
            progress_state["active"] = False
            return False

        def add_task(self, *_: object, **__: object) -> int:
            return 1

    def fake_build(payload: dict[str, Any], language: str) -> TranscriptionResult:
        assert progress_state["active"] is False
        return TranscriptionResult(
            text=str(payload["text"]),
            segments=[],
            language=language,
            response_id=str(payload["response_id"]),
        )

    monkeypatch.setattr(
        "screenscribe.transcribe.httpx.Client",
        lambda *args, **kwargs: _FakeClient(recorded),
    )
    monkeypatch.setattr("screenscribe.transcribe.Progress", FakeProgress)
    monkeypatch.setattr("screenscribe.transcribe._build_transcription_result", fake_build)
    auth_kwargs = {"api" + "_key": "test-key"}

    result = transcribe_audio(
        audio_path,
        language="en",
        stt_endpoint="https://api.example.com/v1/audio/transcriptions",
        stt_model="test-model",
        **auth_kwargs,
    )

    assert result.text == "Browser upload transcript"
