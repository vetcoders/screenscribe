"""Checkpoint serialization round-trip regression tests.

Pins that resumable state survives a save/load cycle without silently losing
fields the resumed pipeline depends on.
"""

from __future__ import annotations

import json
from pathlib import Path

from screenscribe.checkpoint import (
    checkpoint_valid_for_video,
    create_checkpoint,
    deserialize_transcription,
    get_checkpoint_path,
    load_checkpoint,
    save_checkpoint,
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


def test_checkpoint_validity_binds_transcript_and_preset_inputs(tmp_path: Path) -> None:
    video = tmp_path / "clip.mov"
    video.write_bytes(b"video")
    output = tmp_path / "clip_review"
    inputs = {
        "transcript_source": "ocr",
        "frame_interval": 5.0,
        "preset": "veterinary",
        "categories": ["finding", "observation", "risk", "followup", "other"],
        "keywords": {"finding": ["pacjent"]},
        "analysis_prompt_override": "Focus on explicit narration.",
    }
    checkpoint = create_checkpoint(video, output, "pl", analysis_inputs=inputs)

    assert checkpoint_valid_for_video(checkpoint, video, output, "pl", analysis_inputs=dict(inputs))

    changed_source = {**inputs, "transcript_source": "audio", "frame_interval": None}
    assert not checkpoint_valid_for_video(
        checkpoint, video, output, "pl", analysis_inputs=changed_source
    )

    changed_interval = {**inputs, "frame_interval": 2.5}
    assert not checkpoint_valid_for_video(
        checkpoint, video, output, "pl", analysis_inputs=changed_interval
    )

    changed_preset = {**inputs, "preset": "programming"}
    assert not checkpoint_valid_for_video(
        checkpoint, video, output, "pl", analysis_inputs=changed_preset
    )

    changed_keywords = {**inputs, "keywords": {"finding": ["changed phrase"]}}
    assert not checkpoint_valid_for_video(
        checkpoint, video, output, "pl", analysis_inputs=changed_keywords
    )


def test_checkpoint_roundtrip_preserves_analysis_inputs(tmp_path: Path) -> None:
    video = tmp_path / "clip.mov"
    video.write_bytes(b"video")
    output = tmp_path / "clip_review"
    inputs = {
        "transcript_source": "ocr",
        "frame_interval": 2.5,
        "preset": "custom",
        "categories": ["regression", "copy"],
        "keywords": {"regression": ["used to work"], "copy": ["typo"]},
        "analysis_prompt_override": "",
    }

    save_checkpoint(
        create_checkpoint(video, output, "pl", analysis_inputs=inputs),
        output,
    )
    restored = load_checkpoint(output)

    assert restored is not None
    assert restored.analysis_inputs == inputs


def test_checkpoint_roundtrip_preserves_processing_provenance(tmp_path: Path) -> None:
    video = tmp_path / "clip.mov"
    video.write_bytes(b"video")
    output = tmp_path / "clip_review"
    checkpoint = create_checkpoint(video, output, "pl")
    checkpoint.processing_provenance = {
        "llm": {"host": "api.x.ai", "protocol": "responses", "provider": "xai"}
    }

    save_checkpoint(checkpoint, output)
    restored = load_checkpoint(output)

    assert restored is not None
    assert restored.processing_provenance == checkpoint.processing_provenance


def test_current_schema_checkpoint_without_receipt_loads_as_unproven(tmp_path: Path) -> None:
    video = tmp_path / "clip.mov"
    video.write_bytes(b"video")
    output = tmp_path / "clip_review"
    checkpoint = create_checkpoint(video, output, "pl")
    save_checkpoint(checkpoint, output)
    path = get_checkpoint_path(output)
    stored = json.loads(path.read_text(encoding="utf-8"))
    stored.pop("processing_provenance", None)
    path.write_text(json.dumps(stored), encoding="utf-8")

    restored = load_checkpoint(output)

    assert restored is not None
    assert restored.processing_provenance == {}
