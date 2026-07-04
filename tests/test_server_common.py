"""Tests for the shared STT helpers in ``server_common``.

- :func:`read_upload_capped` (P2-10) aborts a chunked read the moment the
  running total crosses the cap, closing the unbounded-``await read()`` hole
  that survived a lying/absent Content-Length.
- :func:`serialize_stt_result` (P3-12, SYS-2 server half) emits one key-set
  for both the analyze and review servers, so the two STT surfaces can no
  longer diverge.

The repo has no async-test plugin configured, so the coroutine helper is
driven through ``asyncio.run`` directly rather than via a pytest marker.
"""

from __future__ import annotations

import asyncio
import io
from typing import Any

import pytest
from fastapi import HTTPException, UploadFile

from screenscribe.server_common import read_upload_capped, serialize_stt_result
from screenscribe.transcribe import Segment, TranscriptionResult


def _upload(data: bytes) -> UploadFile:
    return UploadFile(filename="recording.webm", file=io.BytesIO(data))


def test_read_upload_capped_returns_full_body_under_cap() -> None:
    body = b"voice" * 100
    result = asyncio.run(read_upload_capped(_upload(body), max_bytes=10_000))
    assert result == body


def test_read_upload_capped_aborts_without_buffering_whole_body() -> None:
    """A body over the cap is rejected with 413 even though the UploadFile
    (BytesIO, no Content-Length) never declares its size — proving the cap is
    enforced chunk-wise, not by reading the whole payload first."""
    body = b"x" * 5000
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(read_upload_capped(_upload(body), max_bytes=1024))
    assert excinfo.value.status_code == 413
    assert "limit" in excinfo.value.detail.lower()


def test_read_upload_capped_accepts_body_exactly_at_cap() -> None:
    body = b"a" * 2048
    result = asyncio.run(read_upload_capped(_upload(body), max_bytes=2048))
    assert result == body


def _result() -> TranscriptionResult:
    return TranscriptionResult(
        text="Hello there",
        segments=[Segment(id=1, start=0.0, end=1.5, text="Hello there")],
        language="pl",
        response_id="resp_x",
    )


def test_serialize_stt_result_emits_canonical_key_set() -> None:
    payload = serialize_stt_result(_result())
    assert set(payload) == {"text", "segments", "response_id", "language"}
    assert payload["text"] == "Hello there"
    assert payload["language"] == "pl"
    assert payload["response_id"] == "resp_x"
    assert payload["segments"] == [{"start": 0.0, "end": 1.5, "text": "Hello there"}]


def test_serialize_stt_result_adds_quality_warning_only_when_present() -> None:
    assert "quality_warning" not in serialize_stt_result(_result())

    with_warning = serialize_stt_result(_result(), quality_warning="low confidence")
    assert with_warning["quality_warning"] == "low confidence"
    # Empty/None warnings must not introduce the key.
    assert "quality_warning" not in serialize_stt_result(_result(), quality_warning="")
    assert "quality_warning" not in serialize_stt_result(_result(), quality_warning=None)


# --- BH18: fail-fast on auth/missing-key (no pointless ffmpeg-retry) --------

import httpx  # noqa: E402

from screenscribe.config import ScreenScribeConfig  # noqa: E402
from screenscribe.server_common import transcribe_browser_audio  # noqa: E402


def _stt_config() -> ScreenScribeConfig:
    return ScreenScribeConfig(
        api_key="test-key",  # pragma: allowlist secret
        vision_api_key="test-key",  # pragma: allowlist secret
        stt_endpoint="https://api.example.com/v1/audio/transcriptions",
        stt_model="test-model",
        llm_model="programmer",
        vision_model="programmer",
    )


def _auth_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://api.example.com/v1/audio/transcriptions")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError("auth failure", request=request, response=response)


def _install_fallback_recorder(monkeypatch: pytest.MonkeyPatch, fallback_calls: list[str]) -> None:
    def fake_normalize(path: Any) -> Any:
        fallback_calls.append("normalize")
        return path

    monkeypatch.setattr("screenscribe.audio.normalize_audio_for_stt", fake_normalize)
    monkeypatch.setattr(
        "screenscribe.transcribe.transcribe_audio",
        lambda *a, **k: fallback_calls.append("transcribe_audio"),
    )


def test_bh18_auth_error_does_not_trigger_ffmpeg_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 401/403 from the STT endpoint is not a format problem: the MP3
    normalization fallback must NOT run, and the auth error propagates."""
    fallback_calls: list[str] = []
    _install_fallback_recorder(monkeypatch, fallback_calls)

    def raise_auth(*a: Any, **k: Any) -> Any:
        raise _auth_error(401)

    monkeypatch.setattr("screenscribe.transcribe.transcribe_audio_bytes", raise_auth)

    with pytest.raises(httpx.HTTPStatusError) as excinfo:
        transcribe_browser_audio(
            b"voice", filename="a.webm", content_type="audio/webm", config=_stt_config()
        )
    assert excinfo.value.response.status_code == 401
    assert fallback_calls == []  # no ffmpeg-retry on auth failure


def test_bh18_missing_key_value_error_does_not_trigger_ffmpeg_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing-key ValueError (raised before any HTTP call) fails fast: no
    re-encode + second STT round-trip that would just hit the same wall."""
    fallback_calls: list[str] = []
    _install_fallback_recorder(monkeypatch, fallback_calls)

    def raise_missing_key(*a: Any, **k: Any) -> Any:
        raise ValueError("API key required for cloud STT.")

    monkeypatch.setattr("screenscribe.transcribe.transcribe_audio_bytes", raise_missing_key)

    with pytest.raises(ValueError, match="API key required"):
        transcribe_browser_audio(
            b"voice", filename="a.webm", content_type="audio/webm", config=_stt_config()
        )
    assert fallback_calls == []


def test_bh18_format_error_still_falls_back_to_normalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A genuine format failure (not auth/missing-key) must still trigger the
    MP3-normalization fallback — BH18 must not over-restrict the retry path."""
    fallback_calls: list[str] = []
    _install_fallback_recorder(monkeypatch, fallback_calls)

    def raise_format(*a: Any, **k: Any) -> Any:
        raise RuntimeError("unsupported codec / decode failure")

    monkeypatch.setattr("screenscribe.transcribe.transcribe_audio_bytes", raise_format)

    transcribe_browser_audio(
        b"voice", filename="a.webm", content_type="audio/webm", config=_stt_config()
    )
    assert fallback_calls == ["normalize", "transcribe_audio"]
