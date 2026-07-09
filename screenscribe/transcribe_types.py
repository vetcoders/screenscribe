"""Transcription value types and timeline helpers.

Leaf module: the shared ``Segment``/``TranscriptionResult`` dataclasses plus the
timeline-coverage helpers, split out of ``transcribe`` so the many consumers that
only need the *types* do not transitively pull in the HTTP/STT transport stack.

Intentionally import-light: no ``httpx``, no HTTP, and nothing from
``transcribe`` — this module sits below the transport layer, and
``transcribe`` re-exports every name here for back-compat.
"""

from dataclasses import dataclass

# Timeline-coverage guard thresholds. A long recording whose STT timestamps stop
# well before the real duration signals a compressed/drifted transcript timeline;
# these bound when that guard fires (see transcript_timeline_coverage_is_safe).
MIN_TIMELINE_GUARD_VIDEO_SECONDS = 5 * 60
MIN_TRANSCRIPT_TIMELINE_COVERAGE = 0.80


@dataclass
class Segment:
    """A transcription segment with timing info."""

    id: int
    start: float
    end: float
    text: str
    no_speech_prob: float = 0.0  # Probability that segment contains no speech (0.0-1.0)
    # Whisper decode-confidence metadata (verbose_json), used by the
    # anti-hallucination filter. Defaults are inert: 0.0 never trips the
    # avg_logprob < -1.0 or compression_ratio > 2.4 gates, so old checkpoints and
    # synthetic segments pass through untouched.
    avg_logprob: float = 0.0  # Mean token log-probability (higher = more confident)
    compression_ratio: float = 0.0  # gzip ratio of the text (high = repetitive)


@dataclass
class TranscriptionResult:
    """Full transcription result with segments."""

    text: str
    segments: list[Segment]
    language: str
    response_id: str = ""  # API response ID for conversation chaining to LLM
    # True when the only segment is a synthetic one whose end is estimated from
    # a speaking-rate heuristic (no real STT timestamps). Such timing must not
    # feed the timeline-coverage guard (P3-4).
    timestamps_are_synthetic: bool = False
    # True when filter_hallucinated_segments removed at least one segment. Lets
    # the pipeline tell a genuinely no-speech recording (music/landscape) apart
    # from empty/corrupt audio, so it reports "no speech" instead of an error.
    hallucinations_filtered: bool = False


def transcript_last_segment_end(result: TranscriptionResult) -> float | None:
    """Return the latest STT segment end timestamp, if any."""
    if not result.segments:
        return None
    return max(segment.end for segment in result.segments)


def calculate_transcript_timeline_coverage(
    result: TranscriptionResult,
    duration_seconds: float | None,
) -> float | None:
    """Calculate how much of the video timeline the STT timestamps cover."""
    if duration_seconds is None or duration_seconds <= 0:
        return None
    # Synthetic segment timestamps are estimated from word count, not real STT
    # timing, so a coverage ratio against them is meaningless and would fire a
    # false "compressed STT timeline" warning (P3-4).
    if result.timestamps_are_synthetic:
        return None
    last_segment_end = transcript_last_segment_end(result)
    if last_segment_end is None:
        return None
    return last_segment_end / duration_seconds


def transcript_timeline_coverage_is_safe(
    result: TranscriptionResult,
    duration_seconds: float | None,
    *,
    min_coverage: float = MIN_TRANSCRIPT_TIMELINE_COVERAGE,
    min_video_seconds: float = MIN_TIMELINE_GUARD_VIDEO_SECONDS,
) -> bool:
    """Guard timestamp-based review on long videos against compressed STT timelines."""
    if duration_seconds is None or duration_seconds <= min_video_seconds:
        return True
    coverage = calculate_transcript_timeline_coverage(result, duration_seconds)
    return coverage is None or coverage >= min_coverage
