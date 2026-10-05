"""xAI (api.x.ai) REST provider: speech-to-text and text-to-speech.

Wire contract from docs.x.ai (fetched 2026-09-10):

- ``POST /v1/stt`` multipart: ``file`` (or ``url``), optional ``language``,
  ``format``, ``diarize``, repeatable ``keyterm``, ``vad_threshold``,
  ``audio_format``, ``sample_rate``. No ``model`` and no ``response_format``.
  Response: ``{"text", "language", "duration", "words": [{"text", "start",
  "end", "speaker"?}]}``.
- ``POST /v1/tts`` JSON: ``{"text" (<= 15000 chars), "voice_id" (default eve),
  "language" (required), "output_format": {"codec", "sample_rate", "bit_rate"?},
  "speed" (0.7-1.5)}`` -> raw audio bytes with the codec's content type.
- ``GET /v1/tts/voices`` -> ``{"voices": [{"voice_id", "name"}]}``.

Auth is ``Authorization: Bearer <key>``. This module is import-light on purpose
(``httpx`` + ``transcribe_types`` only) so ``transcribe`` can import it without
a cycle.
"""

from __future__ import annotations

from typing import Any

import httpx

from ..transcribe_types import Segment, TranscriptionResult

XAI_API_BASE = "https://api.x.ai"
XAI_STT_ENDPOINT = f"{XAI_API_BASE}/v1/stt"
XAI_TTS_ENDPOINT = f"{XAI_API_BASE}/v1/tts"
XAI_TTS_VOICES_ENDPOINT = f"{XAI_API_BASE}/v1/tts/voices"
XAI_STT_LIVE_ENDPOINT = "wss://api.x.ai/v1/stt"

# Word-to-segment grouping: a new segment starts on a pause longer than this or
# once a segment holds this many words. Values chosen to mirror the granularity
# of Whisper ``verbose_json`` segments the downstream detection expects.
SEGMENT_GAP_SECONDS = 0.8
SEGMENT_MAX_WORDS = 12

TTS_MAX_TEXT_CHARS = 15_000
TTS_CODECS = ("mp3", "wav", "pcm", "mulaw", "alaw")
TTS_SPEED_RANGE = (0.7, 1.5)
TTS_CONTENT_TYPES = {
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "pcm": "audio/L16",
    "mulaw": "audio/basic",
    "alaw": "audio/alaw",
}

STT_TIMEOUT_SECONDS = 600.0
TTS_TIMEOUT_SECONDS = 120.0


def _finite_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def group_words_into_segments(
    words: list[Any],
    *,
    gap_seconds: float = SEGMENT_GAP_SECONDS,
    max_words: int = SEGMENT_MAX_WORDS,
) -> list[Segment]:
    """Group xAI ``words`` (``{"text","start","end"}``) into timed segments.

    A segment closes when the next word starts more than ``gap_seconds`` after
    the previous word ended, or when it already holds ``max_words`` words.
    Entries without a text or with unparseable timing are skipped.
    """
    segments: list[Segment] = []
    current: list[tuple[str, float, float]] = []

    def flush() -> None:
        if not current:
            return
        segments.append(
            Segment(
                id=len(segments),
                start=current[0][1],
                end=max(end for _, _, end in current),
                text=" ".join(text for text, _, _ in current),
            )
        )
        current.clear()

    for raw in words:
        if not isinstance(raw, dict):
            continue
        text = str(raw.get("text", "")).strip()
        start = _finite_float(raw.get("start"))
        end = _finite_float(raw.get("end"))
        if not text or start is None or end is None:
            continue
        if current and (start - current[-1][2] > gap_seconds or len(current) >= max_words):
            flush()
        current.append((text, start, end))
    flush()
    return segments


def _client(transport: httpx.BaseTransport | None, timeout: float) -> httpx.Client:
    return httpx.Client(timeout=timeout, transport=transport)


def transcribe_file_xai(
    audio_bytes: bytes,
    filename: str,
    *,
    api_key: str,
    language: str | None,
    endpoint: str = XAI_STT_ENDPOINT,
    diarize: bool = False,
    keyterms: tuple[str, ...] | list[str] = (),
    content_type: str = "application/octet-stream",
    transport: httpx.BaseTransport | None = None,
) -> TranscriptionResult:
    """Transcribe one audio file through ``POST /v1/stt``.

    Returns a ``TranscriptionResult`` whose segments carry REAL word timing
    (``timestamps_are_synthetic=False``) whenever the response has ``words``.
    Without words the whole text becomes one speaking-rate-estimated segment,
    flagged synthetic like the OpenAI-compatible path does.
    """
    if not audio_bytes:
        raise ValueError("Audio payload is empty")
    if not api_key:
        raise ValueError("API key required for xAI STT (set SCREENSCRIBE_STT_API_KEY)")

    # ``file`` goes last: the docs require it to be the final multipart field.
    data: dict[str, str | list[str]] = {}
    if language:
        data["language"] = language
    if diarize:
        data["diarize"] = "true"
    terms = [term.strip() for term in keyterms if term and term.strip()]
    if terms:
        data["keyterm"] = terms  # repeatable field: httpx emits one part per value
    files = {"file": (filename, audio_bytes, content_type)}
    headers = {"Authorization": f"Bearer {api_key}"}

    with _client(transport, STT_TIMEOUT_SECONDS) as client:
        response = client.post(endpoint, data=data, files=files, headers=headers)
        response.raise_for_status()
        payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("xAI STT returned unexpected payload shape")

    full_text = str(payload.get("text", "")).strip()
    raw_words = payload.get("words")
    segments = group_words_into_segments(raw_words) if isinstance(raw_words, list) else []
    timestamps_are_synthetic = False
    if not segments and full_text:
        word_count = len(full_text.split())
        segments = [Segment(id=0, start=0.0, end=(word_count / 150) * 60, text=full_text)]
        timestamps_are_synthetic = True

    detected = payload.get("language")
    if not isinstance(detected, str) or not detected.strip():
        detected = language or ""
    return TranscriptionResult(
        text=full_text,
        segments=segments,
        language=detected,
        timestamps_are_synthetic=timestamps_are_synthetic,
    )


def synthesize_speech_xai(
    text: str,
    *,
    api_key: str,
    language: str,
    voice_id: str = "eve",
    codec: str = "mp3",
    sample_rate: int = 24000,
    speed: float = 1.0,
    endpoint: str = XAI_TTS_ENDPOINT,
    transport: httpx.BaseTransport | None = None,
) -> tuple[bytes, str]:
    """Synthesize ``text`` through ``POST /v1/tts``; return ``(audio_bytes, content_type)``.

    ``with_timestamps`` is intentionally not exposed: that variant returns a
    JSON envelope with base64 audio instead of raw bytes.
    """
    if not text or not text.strip():
        raise ValueError("TTS text is empty")
    if len(text) > TTS_MAX_TEXT_CHARS:
        raise ValueError(f"TTS text exceeds {TTS_MAX_TEXT_CHARS} characters ({len(text)})")
    if not language:
        raise ValueError("TTS language is required (BCP-47 code such as en, pl, or auto)")
    if codec not in TTS_CODECS:
        raise ValueError(f"Unsupported TTS codec {codec!r}; choose one of {', '.join(TTS_CODECS)}")
    low, high = TTS_SPEED_RANGE
    if not (low <= speed <= high):
        raise ValueError(f"TTS speed must be within {low}-{high} (got {speed})")
    if not api_key:
        raise ValueError("API key required for xAI TTS (set SCREENSCRIBE_TTS_API_KEY)")

    body: dict[str, Any] = {
        "text": text,
        "voice_id": voice_id or "eve",
        "language": language,
        "output_format": {"codec": codec, "sample_rate": sample_rate},
        "speed": speed,
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    with _client(transport, TTS_TIMEOUT_SECONDS) as client:
        response = client.post(endpoint, json=body, headers=headers)
        response.raise_for_status()
        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip()
        return response.content, content_type or TTS_CONTENT_TYPES.get(codec, "")


def list_voices_xai(
    *,
    api_key: str,
    endpoint: str = XAI_TTS_VOICES_ENDPOINT,
    transport: httpx.BaseTransport | None = None,
) -> list[dict[str, Any]]:
    """Return the ``voices`` array from ``GET /v1/tts/voices``."""
    if not api_key:
        raise ValueError("API key required for xAI TTS (set SCREENSCRIBE_TTS_API_KEY)")
    headers = {"Authorization": f"Bearer {api_key}"}
    with _client(transport, TTS_TIMEOUT_SECONDS) as client:
        response = client.get(endpoint, headers=headers)
        response.raise_for_status()
        payload = response.json()
    voices = payload.get("voices") if isinstance(payload, dict) else payload
    if not isinstance(voices, list):
        raise RuntimeError("xAI voices endpoint returned unexpected payload shape")
    return [voice for voice in voices if isinstance(voice, dict)]
