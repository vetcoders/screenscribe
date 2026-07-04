"""
Screenscribe - Turn screen recordings into actionable engineering reports.

Extract bugs, changes, and action items from video walkthroughs.
Built by Vetcoders
"""

from importlib.metadata import PackageNotFoundError, version

try:
    # Single source of truth: the version declared in pyproject.toml, surfaced
    # through the installed distribution metadata. Avoids the dual-maintenance
    # drift of a hardcoded literal that has to be kept in sync by hand.
    __version__ = version("screenscribe")
except PackageNotFoundError:  # running from a source tree without installed metadata
    __version__ = "0.0.0+dev"

# Export key modules for external use
from .html_pro import render_html_report_pro
from .vtt_generator import (
    SubtitleEntry,
    format_display_timestamp,
    generate_vtt_data_url,
    generate_webvtt,
    generate_webvtt_with_cue_settings,
    seconds_to_vtt_timestamp,
    segments_to_subtitle_entries,
)

__all__ = [
    "SubtitleEntry",
    "__version__",
    "format_display_timestamp",
    "generate_vtt_data_url",
    "generate_webvtt",
    "generate_webvtt_with_cue_settings",
    "render_html_report_pro",
    "seconds_to_vtt_timestamp",
    "segments_to_subtitle_entries",
]
