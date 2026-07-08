"""WebVTT cue-text escaping (report-render hardening).

Raw ``segment.text`` is STT output that reaches the browser's WebVTT tokenizer
via ``<track src="data:text/vtt;...">``. Special characters must be escaped so
narration like ``AT&T`` / ``<div>`` / ``x --> y`` cannot truncate a cue or
desync the timeline.
"""

from __future__ import annotations

from screenscribe.transcribe import Segment
from screenscribe.vtt_generator import (
    escape_cue_text,
    generate_webvtt,
    generate_webvtt_with_cue_settings,
)


def test_escape_cue_text_escapes_amp_and_angle_brackets() -> None:
    assert escape_cue_text("a < b & c > d") == "a &lt; b &amp; c &gt; d"


def test_escape_cue_text_neutralizes_arrow_sequence() -> None:
    # A literal --> would be read as a timestamp separator; escaping > kills it.
    escaped = escape_cue_text("x --> y")
    assert "-->" not in escaped
    assert escaped == "x --&gt; y"


def test_escape_cue_text_collapses_internal_blank_lines() -> None:
    # A blank line terminates a cue; internal blanks must not split one cue.
    assert escape_cue_text("line one\n\n\nline two") == "line one\nline two"


def test_generate_webvtt_escapes_cue_body() -> None:
    vtt = generate_webvtt([Segment(id=1, start=0.0, end=1.0, text="a < b & c")])
    assert "&lt;" in vtt
    assert "&amp;" in vtt
    # The raw special char never survives in the cue body.
    assert "a < b & c" not in vtt


def test_generate_webvtt_no_bare_arrow_in_cue() -> None:
    vtt = generate_webvtt([Segment(id=1, start=0.0, end=1.0, text="x --> y")])
    lines = vtt.splitlines()
    # The only "-->" allowed is the real timestamp line; the cue body has none.
    arrow_lines = [ln for ln in lines if "-->" in ln]
    assert len(arrow_lines) == 1
    assert arrow_lines[0].startswith("00:00:00.000")


def test_generate_webvtt_with_cue_settings_escapes_cue_body() -> None:
    vtt = generate_webvtt_with_cue_settings([Segment(id=1, start=0.0, end=1.0, text="AT&T <tag>")])
    assert "&amp;" in vtt
    assert "&lt;tag&gt;" in vtt
    assert "AT&T <tag>" not in vtt
