"""Bug and change detection from transcripts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console

from .keywords import KeywordsConfig
from .transcribe import Segment, TranscriptionResult

console = Console()

# Global keywords config (lazy loaded)
_keywords_config: KeywordsConfig | None = None


def get_keywords_config(keywords_file: Path | None = None) -> KeywordsConfig:
    """Get or load keywords configuration."""
    global _keywords_config
    if _keywords_config is None or keywords_file is not None:
        _keywords_config = KeywordsConfig.load(keywords_file)
    return _keywords_config


def reset_keywords_config() -> None:
    """Reset keywords config (useful for testing)."""
    global _keywords_config
    _keywords_config = None


# Load defaults for module-level access (backward compatibility)
_default_config = KeywordsConfig.load()
BUG_KEYWORDS: list[str] = _default_config.bug
CHANGE_KEYWORDS: list[str] = _default_config.change
UI_KEYWORDS: list[str] = _default_config.ui


@dataclass
class Detection:
    """A detected issue or change request."""

    segment: Segment
    category: str  # "bug", "change", "ui"
    keywords_found: list[str]
    context: str  # Extended context from surrounding segments


def detect_issues(
    transcription: TranscriptionResult,
    context_window: int = 2,
    keywords_file: Path | None = None,
) -> list[Detection]:
    """
    Detect bugs and change requests in transcription via keyword regex matching.

    INTERNAL / DEBUG HELPER -- this is no longer wired into any CLI path.
    Detection is always the LLM semantic prefilter; keyword-regex detection is
    not a product mode (there is no ``--keywords-only`` flag). Keywords feed the
    AI as prompt hints instead (see ``semantic_filter.semantic_prefilter`` and
    ``keywords.format_keywords_hint``). This function is retained only as a
    library/debug utility with its own test coverage; do not reintroduce it as
    a detection mechanism.

    Args:
        transcription: The transcription result
        context_window: Number of segments before/after to include as context
        keywords_file: Optional path to custom keywords YAML file

    Returns:
        List of detections with category and context
    """
    detections = []
    segments = transcription.segments

    # Load keywords (custom or default)
    keywords = get_keywords_config(keywords_file)
    bug_keywords = keywords.bug
    change_keywords = keywords.change
    ui_keywords = keywords.ui

    console.print("[blue]Analyzing transcript for issues...[/]")
    console.print(f"[dim]{keywords.summary()}[/]")

    for i, segment in enumerate(segments):
        text_lower = segment.text.lower()
        found_keywords = []
        category = None

        # Check for bugs
        for pattern in bug_keywords:
            if re.search(pattern, text_lower):
                found_keywords.append(pattern)
                category = "bug"

        # Check for change requests
        for pattern in change_keywords:
            if re.search(pattern, text_lower):
                found_keywords.append(pattern)
                if category is None:
                    category = "change"

        # Check for UI-related
        for pattern in ui_keywords:
            if re.search(pattern, text_lower):
                found_keywords.append(pattern)
                if category is None:
                    category = "ui"

        if category and found_keywords:
            # Build context from surrounding segments
            start_idx = max(0, i - context_window)
            end_idx = min(len(segments), i + context_window + 1)
            context_segments = segments[start_idx:end_idx]
            context = " ".join(s.text for s in context_segments)

            detections.append(
                Detection(
                    segment=segment,
                    category=category,
                    keywords_found=list(set(found_keywords)),
                    context=context,
                )
            )

    # Merge consecutive detections
    merged = merge_consecutive_detections(detections)

    console.print(
        f"[green]Found {len(merged)} issues:[/] "
        f"{sum(1 for d in merged if d.category == 'bug')} bugs, "
        f"{sum(1 for d in merged if d.category == 'change')} changes, "
        f"{sum(1 for d in merged if d.category == 'ui')} UI issues"
    )

    return merged


def merge_consecutive_detections(
    detections: list[Detection], max_gap: float = 5.0
) -> list[Detection]:
    """
    Merge consecutive detections that are close in time.

    Args:
        detections: List of detections
        max_gap: Maximum gap in seconds to merge

    Returns:
        Merged list of detections
    """
    if not detections:
        return []

    merged = []
    current = detections[0]

    for detection in detections[1:]:
        gap = detection.segment.start - current.segment.end

        if gap <= max_gap and detection.category == current.category:
            # Merge: extend end time, combine keywords and context
            current = Detection(
                segment=Segment(
                    id=current.segment.id,
                    start=current.segment.start,
                    end=detection.segment.end,
                    text=f"{current.segment.text} {detection.segment.text}",
                    # W1A-16: carry STT confidence metadata through the merge
                    # instead of silently resetting it to the 0.0 defaults.
                    no_speech_prob=current.segment.no_speech_prob,
                    avg_logprob=current.segment.avg_logprob,
                    compression_ratio=current.segment.compression_ratio,
                ),
                category=current.category,
                keywords_found=list(set(current.keywords_found + detection.keywords_found)),
                context=f"{current.context} ... {detection.context}",
            )
        else:
            merged.append(current)
            current = detection

    merged.append(current)
    return merged


def format_timestamp(seconds: float) -> str:
    """Format seconds as MM:SS."""
    minutes = int(seconds // 60)
    secs = int(seconds % 60)
    return f"{minutes:02d}:{secs:02d}"
