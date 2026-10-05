"""xAI REST provider: STT (``POST /v1/stt``) and TTS (``POST /v1/tts``).

Wire contract per docs.x.ai (fetched 2026-09-10). Transport is mocked with
``httpx.MockTransport`` -- no network.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from screenscribe.providers.xai import (
    XAI_STT_ENDPOINT,
    XAI_TTS_ENDPOINT,
    XAI_TTS_VOICES_ENDPOINT,
    group_words_into_segments,
    list_voices_xai,
    synthesize_speech_xai,
    transcribe_file_xai,
)
from screenscribe.transcribe_types import TranscriptionResult

KEY = "xai-" + "test-key"  # pragma: allowlist secret


def _word(text: str, start: float, end: float) -> dict[str, Any]:
    return {"text": text, "start": start, "end": end}


def test_group_words_splits_on_gap_and_word_budget() -> None:
    words = [_word(f"w{i}", i * 0.3, i * 0.3 + 0.2) for i in range(14)]
    # A 2 s pause after the 14th word starts a new segment.
    words += [_word("after", 6.0, 6.3), _word("pause", 6.4, 6.8)]

    segments = group_words_into_segments(words)

    assert [len(s.text.split()) for s in segments] == [12, 2, 2]
    assert segments[0].start == 0.0
    assert segments[0].end == pytest.approx(11 * 0.3 + 0.2)
    assert segments[2].text == "after pause"
    assert segments[2].start == 6.0
    assert segments[2].end == 6.8
    assert [s.id for s in segments] == [0, 1, 2]


def test_group_words_skips_malformed_entries() -> None:
    words: list[Any] = [_word("ok", 0.0, 0.5), "junk", {"text": "no-timing"}, None]
    segments = group_words_into_segments(words)
    assert [s.text for s in segments] == ["ok"]


def test_transcribe_file_xai_sends_documented_multipart_and_builds_real_segments() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["content_type"] = request.headers.get("content-type", "")
        seen["body"] = request.content
        return httpx.Response(
            200,
            json={
                "text": "hello there world",
                "language": "en",
                "duration": 1.9,
                "words": [
                    _word("hello", 0.1, 0.4),
                    _word("there", 0.5, 0.8),
                    _word("world", 1.7, 1.9),
                ],
            },
        )

    result = transcribe_file_xai(
        b"RIFF-fake",
        "clip.wav",
        api_key=KEY,
        language="en",
        diarize=True,
        keyterms=("Loctree", "Vibecrafted"),
        transport=httpx.MockTransport(handler),
    )

    assert seen["url"] == XAI_STT_ENDPOINT
    assert seen["auth"] == f"Bearer {KEY}"
    assert seen["content_type"].startswith("multipart/form-data")
    body = seen["body"]
    assert b'name="file"; filename="clip.wav"' in body
    assert b'name="language"\r\n\r\nen' in body
    assert b'name="diarize"\r\n\r\ntrue' in body
    assert body.count(b'name="keyterm"') == 2
    # xAI takes no model and no response_format.
    assert b'name="model"' not in body
    assert b'name="response_format"' not in body

    assert isinstance(result, TranscriptionResult)
    assert result.text == "hello there world"
    assert result.language == "en"
    assert result.timestamps_are_synthetic is False
    assert [s.text for s in result.segments] == ["hello there", "world"]
    assert result.segments[1].start == 1.7
    assert result.segments[1].end == 1.9


def test_transcribe_file_xai_language_is_optional() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content
        return httpx.Response(200, json={"text": "", "language": "pl", "words": []})

    result = transcribe_file_xai(
        b"x", "a.mp3", api_key=KEY, language=None, transport=httpx.MockTransport(handler)
    )
    assert b'name="language"' not in seen["body"]
    assert result.segments == []
    assert result.language == "pl"


def test_transcribe_file_xai_without_words_falls_back_to_synthetic_segment() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"text": "only text here", "language": "en"})

    result = transcribe_file_xai(
        b"x", "a.mp3", api_key=KEY, language="en", transport=httpx.MockTransport(handler)
    )
    assert result.timestamps_are_synthetic is True
    assert len(result.segments) == 1
    assert result.segments[0].text == "only text here"


def test_transcribe_file_xai_rejects_empty_payload_and_missing_key() -> None:
    with pytest.raises(ValueError, match="empty"):
        transcribe_file_xai(b"", "a.mp3", api_key=KEY, language="en")
    with pytest.raises(ValueError, match="API key"):
        transcribe_file_xai(b"x", "a.mp3", api_key="", language="en")


def test_transcribe_file_xai_raises_http_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "bad key"})

    with pytest.raises(httpx.HTTPStatusError):
        transcribe_file_xai(
            b"x", "a.mp3", api_key=KEY, language="en", transport=httpx.MockTransport(handler)
        )


def test_synthesize_speech_xai_posts_documented_json_and_returns_audio() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["json"] = json.loads(request.content)
        return httpx.Response(200, content=b"ID3mp3bytes", headers={"content-type": "audio/mpeg"})

    audio, content_type = synthesize_speech_xai(
        "Cześć świecie",
        api_key=KEY,
        language="pl",
        voice_id="ara",
        codec="mp3",
        sample_rate=24000,
        speed=1.1,
        transport=httpx.MockTransport(handler),
    )

    assert seen["url"] == XAI_TTS_ENDPOINT
    assert seen["auth"] == f"Bearer {KEY}"
    assert seen["json"] == {
        "text": "Cześć świecie",
        "voice_id": "ara",
        "language": "pl",
        "output_format": {"codec": "mp3", "sample_rate": 24000},
        "speed": 1.1,
    }
    assert audio == b"ID3mp3bytes"
    assert content_type == "audio/mpeg"


def test_synthesize_speech_xai_validates_inputs() -> None:
    with pytest.raises(ValueError, match="15"):
        synthesize_speech_xai("x" * 15_001, api_key=KEY, language="en")
    with pytest.raises(ValueError, match="empty"):
        synthesize_speech_xai("   ", api_key=KEY, language="en")
    with pytest.raises(ValueError, match="speed"):
        synthesize_speech_xai("hi", api_key=KEY, language="en", speed=2.0)
    with pytest.raises(ValueError, match="codec"):
        synthesize_speech_xai("hi", api_key=KEY, language="en", codec="ogg")
    with pytest.raises(ValueError, match="language"):
        synthesize_speech_xai("hi", api_key=KEY, language="")


def test_list_voices_xai_returns_voices_array() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == XAI_TTS_VOICES_ENDPOINT
        assert request.method == "GET"
        return httpx.Response(
            200, json={"voices": [{"voice_id": "eve", "name": "Eve"}, {"voice_id": "ara"}]}
        )

    voices = list_voices_xai(api_key=KEY, transport=httpx.MockTransport(handler))
    assert [v["voice_id"] for v in voices] == ["eve", "ara"]
