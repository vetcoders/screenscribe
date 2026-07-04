"""Checkpoint serialization round-trip regression tests.

Pins that resumable state survives a save/load cycle without silently losing
fields the resumed pipeline depends on.
"""

from __future__ import annotations

from screenscribe.checkpoint import (
    deserialize_transcription,
    serialize_transcription,
)
from screenscribe.transcribe import Segment, TranscriptionResult


def _transcription(response_id: str = "") -> TranscriptionResult:
    return TranscriptionResult(
        text="hello world",
        segments=[Segment(id=0, start=0.0, end=1.5, text="hello world", no_speech_prob=0.1)],
        language="en",
        response_id=response_id,
    )


def test_serialize_transcription_roundtrip_preserves_response_id() -> None:
    """BH31: the STT response_id drives LLM conversation chaining (semantic
    prefilter). It must survive a checkpoint round-trip so --resume keeps the
    same chained context instead of starting a fresh, uncoupled LLM thread."""
    original = _transcription(response_id="resp_stt_abc123")

    restored = deserialize_transcription(serialize_transcription(original))

    assert restored.response_id == "resp_stt_abc123"
    assert restored.text == original.text
    assert restored.language == original.language
    assert [s.text for s in restored.segments] == [s.text for s in original.segments]


def test_serialize_transcription_roundtrip_preserves_synthetic_flag() -> None:
    """P3-4: the synthetic-timestamp flag must survive resume so the coverage
    guard stays disabled for transcripts that never had real STT timing."""
    original = TranscriptionResult(
        text="text only",
        segments=[Segment(id=0, start=0.0, end=10.0, text="text only")],
        language="en",
        timestamps_are_synthetic=True,
    )

    restored = deserialize_transcription(serialize_transcription(original))

    assert restored.timestamps_are_synthetic is True


def test_deserialize_transcription_defaults_missing_response_id() -> None:
    """Old checkpoints written before response_id was persisted must still load."""
    legacy = {
        "text": "hi",
        "language": "en",
        "segments": [{"id": 0, "start": 0.0, "end": 1.0, "text": "hi"}],
    }

    restored = deserialize_transcription(legacy)

    assert restored.response_id == ""
    assert restored.segments[0].no_speech_prob == 0.0
