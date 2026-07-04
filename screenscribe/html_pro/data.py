"""Data preparation for HTML Pro template.

Functions for preparing findings, segments, and other data for the template.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..transcribe import Segment


def generate_report_id(video_name: str, timestamp: str) -> str:
    """Generate a unique report ID based on video name and timestamp.

    Args:
        video_name: Name of the video file
        timestamp: Generation timestamp

    Returns:
        12-character hex hash
    """
    hash_input = f"{video_name}:{timestamp}"
    return hashlib.sha256(hash_input.encode()).hexdigest()[:12]


def _escape_for_script_embed(json_text: str) -> str:
    """Make serialized JSON safe to embed inside a <script> block.

    json.dumps leaves "</", "<!--" and "<script" untouched, so model- or
    transcript-controlled text could close the surrounding <script> tag and
    inject live markup. "<" only ever appears inside JSON strings, so
    replacing it with its unicode escape keeps the value byte-identical
    after parsing while staying inert to the HTML parser. U+2028/U+2029 are
    escaped because they are line terminators in JavaScript source.
    """
    return (
        json_text.replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    )


def prepare_findings_json(findings: list[dict[str, Any]]) -> str:
    """Prepare findings data as JSON for embedding in HTML.

    Args:
        findings: List of finding dictionaries

    Returns:
        JSON string with "<" escaped so it cannot break out of a <script> tag
    """
    return _escape_for_script_embed(json.dumps(findings, ensure_ascii=False))


def prepare_segments_json(segments: list[Segment] | None) -> str:
    """Prepare transcript segments as JSON for the video player.

    Args:
        segments: List of transcript Segment objects (or None)

    Returns:
        JSON array of segment objects with id, start, end, text; "<" is
        escaped so segment text cannot break out of a <script> tag
    """
    if not segments:
        return "[]"

    segment_data = [
        {
            "id": seg.id,
            "start": seg.start,
            "end": seg.end,
            "text": seg.text,
        }
        for seg in segments
    ]
    return _escape_for_script_embed(json.dumps(segment_data, ensure_ascii=False))
