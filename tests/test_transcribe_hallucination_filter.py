"""Anti-hallucination segment filter (FW-09).

Whisper-family STT fabricates plausible outro captions ("thanks for watching")
on music/silence, with CONFIDENT metadata, so no single field is decisive. The
filter combines Whisper's own decode gates (no_speech_prob / avg_logprob /
compression_ratio) with two physically-grounded timing signals measured on real
hallucinated output from Utah001.mp4 (46s of background music, zero speech):

    seg0: 0.0-29.98s  "Wielkie dzięki i do zobaczenia w kolejnych odcinkach."
          no_speech_prob=0.014  avg_logprob=-0.249  compression_ratio=0.87
    seg1: 30.0-59.98s "Dziękuje za oglądanie!"
          no_speech_prob=0.581  avg_logprob=-0.369  compression_ratio=0.75

Neither is caught by the canonical confidence gate (Whisper is confident about
its hallucination), so the filter also drops (a) segments timed past the real
audio end and (b) a short caption stretched across a long non-speech decode
window (implausibly low word rate).
"""

from __future__ import annotations

from screenscribe.transcribe import (
    Segment,
    TranscriptionResult,
    filter_hallucinated_segments,
)


def _speech_segment() -> Segment:
    """A dense, confident real-speech segment that must always survive."""
    return Segment(
        id=0,
        start=0.0,
        end=4.0,
        text="okay so this button in the settings panel does not save my changes",
        no_speech_prob=0.02,
        avg_logprob=-0.28,
        compression_ratio=1.4,
    )


def _result(segments: list[Segment]) -> TranscriptionResult:
    text = " ".join(s.text for s in segments)
    return TranscriptionResult(text=text, segments=segments, language="pl")


def test_real_speech_segment_survives() -> None:
    seg = _speech_segment()
    out = filter_hallucinated_segments(_result([seg]), audio_duration=10.0)

    assert [s.text for s in out.segments] == [seg.text]
    assert out.hallucinations_filtered is False


def test_phantom_timing_dropped() -> None:
    """A segment ending past the real audio end cannot be real STT timing."""
    phantom = Segment(
        id=1,
        start=30.0,
        end=59.98,
        text="Dziękuje za oglądanie!",
        no_speech_prob=0.581,
        avg_logprob=-0.369,
        compression_ratio=0.75,
    )
    out = filter_hallucinated_segments(_result([phantom]), audio_duration=46.37)

    assert out.segments == []
    assert out.hallucinations_filtered is True


def test_stretched_caption_dropped() -> None:
    """A handful of words spread across a ~30s window = non-speech window."""
    stretched = Segment(
        id=0,
        start=0.0,
        end=29.98,
        text="Wielkie dzięki i do zobaczenia w kolejnych odcinkach.",
        no_speech_prob=0.014,
        avg_logprob=-0.249,
        compression_ratio=0.87,
    )
    out = filter_hallucinated_segments(_result([stretched]), audio_duration=46.37)

    assert out.segments == []
    assert out.hallucinations_filtered is True


def test_canonical_confidence_gate_dropped() -> None:
    """Classic dead-silence hallucination: high no_speech + low avg_logprob."""
    dead = Segment(
        id=0,
        start=0.0,
        end=3.0,
        text="Thank you.",
        no_speech_prob=0.92,
        avg_logprob=-1.6,
        compression_ratio=0.9,
    )
    out = filter_hallucinated_segments(_result([dead]), audio_duration=10.0)

    assert out.segments == []
    assert out.hallucinations_filtered is True


def test_compression_ratio_repetition_dropped() -> None:
    """Repetition hallucination: very high compression ratio."""
    repeat = Segment(
        id=0,
        start=0.0,
        end=6.0,
        text="you you you you you you you you you you you you",
        no_speech_prob=0.1,
        avg_logprob=-0.4,
        compression_ratio=3.2,
    )
    out = filter_hallucinated_segments(_result([repeat]), audio_duration=10.0)

    assert out.segments == []
    assert out.hallucinations_filtered is True


def test_utah_full_repro_empties_transcript() -> None:
    """Both measured Utah segments are removed; transcript rebuilt to empty."""
    seg0 = Segment(
        id=0,
        start=0.0,
        end=29.98,
        text="Wielkie dzięki i do zobaczenia w kolejnych odcinkach.",
        no_speech_prob=0.014,
        avg_logprob=-0.249,
        compression_ratio=0.87,
    )
    seg1 = Segment(
        id=1,
        start=30.0,
        end=59.98,
        text="Dziękuje za oglądanie!",
        no_speech_prob=0.581,
        avg_logprob=-0.369,
        compression_ratio=0.75,
    )
    out = filter_hallucinated_segments(_result([seg0, seg1]), audio_duration=46.37)

    assert out.segments == []
    assert out.text == ""
    assert out.hallucinations_filtered is True


def test_mixed_keeps_speech_drops_hallucination() -> None:
    """A real finding survives even when a hallucinated outro is appended."""
    speech = _speech_segment()
    outro = Segment(
        id=1,
        start=40.0,
        end=69.0,
        text="Dziękuję za oglądanie!",
        no_speech_prob=0.2,
        avg_logprob=-0.3,
        compression_ratio=0.8,
    )
    out = filter_hallucinated_segments(_result([speech, outro]), audio_duration=44.0)

    assert [s.text for s in out.segments] == [speech.text]
    assert speech.text in out.text
    assert "oglądanie" not in out.text
    assert out.hallucinations_filtered is True


def test_synthetic_timestamps_skip_filter() -> None:
    """Synthetic (speaking-rate estimated) timings carry no real STT metadata,
    and the estimated end can exceed the real audio; never filter them."""
    result = TranscriptionResult(
        text="short note",
        segments=[Segment(id=0, start=0.0, end=90.0, text="short note")],
        language="pl",
        timestamps_are_synthetic=True,
    )
    out = filter_hallucinated_segments(result, audio_duration=5.0)

    assert [s.text for s in out.segments] == ["short note"]
    assert out.hallucinations_filtered is False


def test_no_duration_still_applies_metadata_gates() -> None:
    """Without audio_duration the phantom-timing gate is skipped, but the
    confidence / repetition / word-rate gates still run."""
    dead = Segment(
        id=0,
        start=0.0,
        end=3.0,
        text="Thank you.",
        no_speech_prob=0.92,
        avg_logprob=-1.6,
        compression_ratio=0.9,
    )
    out = filter_hallucinated_segments(_result([dead]), audio_duration=None)

    assert out.segments == []
    assert out.hallucinations_filtered is True
