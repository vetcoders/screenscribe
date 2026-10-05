"""STT model capability table: pick ``response_format`` from the model name.

Whisper-family models return per-segment timing under ``verbose_json``. OpenAI's
``gpt-transcribe`` / ``gpt-4o-transcribe`` / ``gpt-4o-mini-transcribe`` family
accepts only ``json``/``text`` and rejects ``verbose_json`` with HTTP 400, so a
known model of that family must request ``json`` directly instead of paying one
rejected request per (endpoint, model). Unknown models keep the 400-driven
fallback from commit 9bd7b2b. All STT calls are mocked.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from screenscribe.transcribe import preferred_stt_response_format, transcribe_audio
from tests.test_transcribe_response_format import _FormatSensitiveClient

ENDPOINT = "https://api.example.com/v1/audio/transcriptions"


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("whisper-1", "verbose_json"),
        ("whisper-large-v3-turbo", "verbose_json"),
        ("gpt-transcribe", "json"),
        ("gpt-transcribe-api-ev3", "json"),
        ("gpt-4o-transcribe", "json"),
        ("gpt-4o-mini-transcribe", "json"),
        ("GPT-4o-Transcribe-Diarize", "json"),
        ("some-unknown-stt", "verbose_json"),
        ("", "verbose_json"),
    ],
)
def test_preferred_response_format_by_model(model: str, expected: str) -> None:
    assert preferred_stt_response_format(model) == expected


@pytest.fixture
def audio_path(tmp_path: Path) -> Path:
    path = tmp_path / "audio.mp3"
    path.write_bytes(b"voice-data")
    return path


@pytest.fixture(autouse=True)
def _fresh_refusal_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("screenscribe.transcribe._VERBOSE_JSON_REFUSED", set(), raising=False)


def _install(monkeypatch: pytest.MonkeyPatch, client: Any) -> None:
    monkeypatch.setattr("screenscribe.transcribe.httpx.Client", lambda *a, **k: client)


def test_known_json_only_model_requests_json_directly(
    monkeypatch: pytest.MonkeyPatch, audio_path: Path
) -> None:
    posted: list[str] = []
    _install(monkeypatch, _FormatSensitiveClient(posted, refuse_verbose_json=True))
    auth_kwargs = {"api" + "_key": "test-key"}

    result = transcribe_audio(
        audio_path,
        language="en",
        stt_endpoint=ENDPOINT,
        stt_model="gpt-4o-transcribe",
        **auth_kwargs,
    )

    # No wasted 400: the very first request already asks for json.
    assert posted == ["json"]
    assert result.timestamps_are_synthetic is True


def test_whisper_model_keeps_verbose_json(
    monkeypatch: pytest.MonkeyPatch, audio_path: Path
) -> None:
    posted: list[str] = []
    _install(monkeypatch, _FormatSensitiveClient(posted, refuse_verbose_json=False))
    auth_kwargs = {"api" + "_key": "test-key"}

    result = transcribe_audio(
        audio_path, language="en", stt_endpoint=ENDPOINT, stt_model="whisper-1", **auth_kwargs
    )

    assert posted == ["verbose_json"]
    assert result.timestamps_are_synthetic is False


def test_unknown_model_keeps_the_400_fallback(
    monkeypatch: pytest.MonkeyPatch, audio_path: Path
) -> None:
    posted: list[str] = []
    _install(monkeypatch, _FormatSensitiveClient(posted, refuse_verbose_json=True))
    auth_kwargs = {"api" + "_key": "test-key"}

    transcribe_audio(
        audio_path, language="en", stt_endpoint=ENDPOINT, stt_model="vendor-stt-x", **auth_kwargs
    )

    assert posted == ["verbose_json", "json"]
