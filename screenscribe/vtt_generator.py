"""WebVTT subtitle generation from transcript segments.

Generates standard WebVTT format for video player integration.
Supports timestamp anchoring and segment metadata.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .transcribe import Segment

_CUE_BLANK_LINE_RE = re.compile(r"\n\s*\n+")


def escape_cue_text(text: str) -> str:
    """Escape transcript text so it is safe inside a WebVTT cue payload.

    WebVTT requires ``&`` and ``<`` to be written as ``&amp;`` / ``&lt;`` in cue
    text (raw ``<`` starts a cue-span tag, so ``<div>`` in narration would be
    swallowed; raw ``&`` starts an entity). ``>`` is escaped too, which also
    neutralises the ``-->`` sequence — a literal ``-->`` in a cue would be
    mistaken for a timestamp separator and desync every following cue. Finally a
    blank line terminates a cue, so internal blank lines are collapsed to keep
    multi-line narration inside a single cue instead of splitting it.
    """
    escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return _CUE_BLANK_LINE_RE.sub("\n", escaped)


def seconds_to_vtt_timestamp(seconds: float) -> str:
    """Convert seconds to WebVTT timestamp format HH:MM:SS.mmm.

    Args:
        seconds: Timestamp in seconds (e.g., 125.456)

    Returns:
        Timestamp string in format HH:MM:SS.mmm (e.g., "00:02:05.456")
    """
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds % 60

    return f"{hours:02d}:{minutes:02d}:{secs:06.3f}"


def format_display_timestamp(seconds: float) -> str:
    """Format seconds for human-readable display.

    Args:
        seconds: Timestamp in seconds

    Returns:
        Formatted string like "2:05" or "1:02:05" for longer videos
    """
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)

    if hours > 0:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def generate_webvtt(segments: list[Segment], language: str = "en") -> str:
    """Generate WebVTT content from transcript segments.

    Args:
        segments: List of Segment dataclass instances
        language: Language code for the VTT header

    Returns:
        WebVTT formatted string ready for <track> element

    Example:
        >>> segments = [
        ...     Segment(id=1, start=0.0, end=5.5, text="Hello world"),
        ...     Segment(id=2, start=5.5, end=10.2, text="This is a test"),
        ... ]
        >>> vtt = generate_webvtt(segments)
        >>> print(vtt)
        WEBVTT
        Kind: captions
        Language: pl

        1
        00:00:00.000 --> 00:00:05.500
        Hello world

        2
        00:00:05.500 --> 00:00:10.200
        This is a test
    """
    lines = [
        "WEBVTT",
        "Kind: captions",
        f"Language: {language}",
        "",
    ]

    for segment in segments:
        start = seconds_to_vtt_timestamp(segment.start)
        end = seconds_to_vtt_timestamp(segment.end)

        lines.append(str(segment.id))
        lines.append(f"{start} --> {end}")
        lines.append(escape_cue_text(segment.text))
        lines.append("")

    return "\n".join(lines)


def generate_webvtt_with_cue_settings(
    segments: list[Segment],
    position: str = "50%",
    line: str = "auto",
    align: str = "center",
    *,
    language: str = "en",
) -> str:
    """Generate WebVTT with cue settings for custom positioning.

    Args:
        segments: List of Segment instances
        position: Horizontal position (0%-100%)
        line: Vertical line position ("auto" or number)
        align: Text alignment ("start", "center", "end")

    Returns:
        WebVTT formatted string with cue settings
    """
    lines = [
        "WEBVTT",
        "Kind: captions",
        f"Language: {language}",
        "",
    ]

    for segment in segments:
        start = seconds_to_vtt_timestamp(segment.start)
        end = seconds_to_vtt_timestamp(segment.end)

        cue_settings = f"position:{position} line:{line} align:{align}"
        lines.append(str(segment.id))
        lines.append(f"{start} --> {end} {cue_settings}")
        lines.append(escape_cue_text(segment.text))
        lines.append("")

    return "\n".join(lines)


@dataclass
class SubtitleEntry:
    """Subtitle entry for sidebar list rendering."""

    id: int
    start: float
    end: float
    text: str
    display_start: str
    display_end: str

    @classmethod
    def from_segment(cls, segment: Segment) -> SubtitleEntry:
        """Create SubtitleEntry from a Segment."""
        return cls(
            id=segment.id,
            start=segment.start,
            end=segment.end,
            text=segment.text,
            display_start=format_display_timestamp(segment.start),
            display_end=format_display_timestamp(segment.end),
        )


def segments_to_subtitle_entries(segments: list[Segment]) -> list[SubtitleEntry]:
    """Convert segments to subtitle entries for template rendering."""
    return [SubtitleEntry.from_segment(s) for s in segments]


def generate_vtt_data_url(segments: list[Segment], language: str = "en") -> str:
    """Generate a data URL containing the WebVTT content.

    This allows embedding VTT directly in HTML without external files.

    Args:
        segments: List of Segment instances

    Returns:
        Data URL string for use in <track src="...">
    """
    import base64

    vtt_content = generate_webvtt(segments, language=language)
    vtt_bytes = vtt_content.encode("utf-8")
    b64 = base64.b64encode(vtt_bytes).decode("ascii")

    return f"data:text/vtt;base64,{b64}"
