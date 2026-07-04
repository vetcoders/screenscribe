"""Hallucination-guard word-count basis regression tests (BH24 + BH36).

validate_audio_quality mixes a raw word count with a punctuation-normalized
transcript when deciding whether the STT output is a generic silence
hallucination. Punctuation tokens inflate the raw count and can push a
genuinely short hallucination past the ``word_count <= 8`` gate, defeating the
guard. The count must share the normalized basis it is compared against.
"""

from __future__ import annotations

from screenscribe.transcribe import (
    Segment,
    TranscriptionResult,
    calculate_transcript_timeline_coverage,
    validate_audio_quality,
)


def _result(text: str) -> TranscriptionResult:
    return TranscriptionResult(
        text=text,
        segments=[Segment(id=0, start=0.0, end=2.0, text=text, no_speech_prob=0.2)],
        language="en",
    )


def test_generic_hallucination_detected_plain() -> None:
    """Baseline: a clean generic phrase is still flagged."""
    is_valid, message, _ = validate_audio_quality(_result("Thank you for watching."))

    assert is_valid is False
    assert message is not None
    assert "hallucination" in message.lower()


def test_punctuation_inflated_hallucination_still_detected() -> None:
    """BH24/BH36: punctuation tokens inflate the raw word count above the
    ``<= 8`` gate while the normalized phrase still matches a generic
    hallucination. Unifying the count basis keeps the guard firing."""
    # Raw split = 10+ tokens; normalized strips . , ! down to "thank you".
    noisy = ". , ! . , ! . , thank you"
    is_valid, message, _ = validate_audio_quality(_result(noisy))

    assert is_valid is False
    assert message is not None
    assert "hallucination" in message.lower()


def test_synthetic_segment_coverage_is_none() -> None:
    """P3-4: a synthetic segment derives its end from an estimated speaking
    rate, not real STT timing. Feeding that fabricated end into the timeline
    coverage guard produces a meaningless ratio that can fire a false
    'compressed STT timeline' warning. Synthetic timestamps must yield None
    coverage so the guard is skipped."""
    result = TranscriptionResult(
        text="some text without real timestamps",
        segments=[Segment(id=0, start=0.0, end=12.0, text="some text without real timestamps")],
        language="en",
        timestamps_are_synthetic=True,
    )

    assert calculate_transcript_timeline_coverage(result, duration_seconds=600.0) is None


def test_real_segment_coverage_is_computed() -> None:
    """Real STT timestamps still produce a coverage ratio."""
    result = TranscriptionResult(
        text="real",
        segments=[Segment(id=0, start=0.0, end=300.0, text="real")],
        language="en",
    )

    assert calculate_transcript_timeline_coverage(result, duration_seconds=600.0) == 0.5
