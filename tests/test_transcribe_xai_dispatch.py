"""``transcribe_audio*`` route to ``providers.xai`` when the STT host is api.x.ai.

The OpenAI-compatible multipart (``model``/``response_format``) never reaches
xAI; the chunked path keeps working because it delegates per chunk to
``transcribe_audio``. No network: the provider call is intercepted.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from screenscribe import transcribe
from screenscribe.transcribe_types import Segment, TranscriptionResult

XAI = "https://api.x.ai/v1/stt"


def _fake_xai(calls: list[dict[str, Any]]) -> Any:
    def fake(audio_bytes: bytes, filename: str, **kwargs: Any) -> TranscriptionResult:
        calls.append({"bytes": audio_bytes, "filename": filename, **kwargs})
        return TranscriptionResult(
            text="xai text",
            segments=[Segment(id=0, start=0.0, end=1.0, text="xai text")],
            language=kwargs.get("language") or "en",
        )

    return fake


def test_is_xai_endpoint_by_host() -> None:
    assert transcribe._is_xai_endpoint(XAI) is True
    assert transcribe._is_xai_endpoint("https://api.openai.com/v1/audio/transcriptions") is False
    assert transcribe._is_xai_endpoint(None) is False


def test_transcribe_audio_bytes_routes_to_xai(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(transcribe, "transcribe_file_xai", _fake_xai(calls))
    monkeypatch.setattr(
        transcribe.httpx, "Client", lambda *a, **k: pytest.fail("OpenAI-style client used")
    )
    auth = {"api" + "_key": "xai-key"}

    result = transcribe.transcribe_audio_bytes(
        b"pcm", "clip.webm", language="pl", stt_endpoint=XAI, stt_model="", **auth
    )

    assert result.text == "xai text"
    assert len(calls) == 1
    assert calls[0]["filename"] == "clip.webm"
    assert calls[0]["endpoint"] == XAI
    assert calls[0]["language"] == "pl"
    assert calls[0]["api_key"] == "xai-key"  # pragma: allowlist secret
    assert calls[0]["content_type"] == "audio/webm"


def test_transcribe_audio_routes_to_xai(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(transcribe, "transcribe_file_xai", _fake_xai(calls))
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"mp3")
    auth = {"api" + "_key": "xai-key"}

    result = transcribe.transcribe_audio(audio, language="en", stt_endpoint=XAI, **auth)

    assert result.timestamps_are_synthetic is False
    assert calls[0]["bytes"] == b"mp3"
    assert calls[0]["filename"] == "a.mp3"


def test_xai_requires_api_key(tmp_path: Path) -> None:
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"mp3")
    with pytest.raises(ValueError, match="API key"):
        transcribe.transcribe_audio(audio, language="en", stt_endpoint=XAI)


def test_chunked_path_delegates_per_chunk_to_xai(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(transcribe, "transcribe_file_xai", _fake_xai(calls))
    audio = tmp_path / "long.wav"
    audio.write_bytes(b"wav")
    chunk_a = tmp_path / "c0.wav"
    chunk_b = tmp_path / "c1.wav"
    chunk_a.write_bytes(b"c0")
    chunk_b.write_bytes(b"c1")
    monkeypatch.setattr("screenscribe.audio.get_audio_duration", lambda _p: 130.0)
    monkeypatch.setattr(
        "screenscribe.audio.split_audio_chunks",
        lambda _p, **_k: [(chunk_a, 0.0), (chunk_b, 60.0)],
    )
    auth = {"api" + "_key": "xai-key"}

    result = transcribe.transcribe_audio_chunked(
        audio, language="en", stt_endpoint=XAI, stt_model="", **auth
    )

    assert [c["bytes"] for c in calls] == [b"c0", b"c1"]
    assert [round(s.start) for s in result.segments] == [0, 60]
