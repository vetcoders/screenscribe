"""Report-render hardening: frame MIME, slot-literal safety, video-path leak.

Each test pins one report-render finding from the quality recon:
* the embedded frame data URI must declare the true media type (JPEG frames),
* a literal ``{slot:...}`` in externally-driven text must not explode the render,
* an absolute local video path must never leak into a shareable HTML report.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from screenscribe.detect import Detection
from screenscribe.html_pro import render_html_report_pro
from screenscribe.report import save_html_report_pro
from screenscribe.transcribe import Segment


def _embedded_findings(html_text: str) -> list[dict[str, Any]]:
    match = re.search(r'<script id="original-findings"[^>]*>(.*?)</script>', html_text, re.DOTALL)
    assert match, "HTML report must embed original-findings JSON"
    return json.loads(match.group(1))


def _detection(
    seg_id: int, start: float, *, text: str = "the save button does nothing"
) -> Detection:
    return Detection(
        segment=Segment(id=seg_id, start=start, end=start + 2.0, text=text),
        category="bug",
        keywords_found=["semantic:bug"],
        context="context",
    )


def _finding_dict(text: str = "finding text") -> dict[str, Any]:
    return {
        "id": 1,
        "category": "ui",
        "timestamp_formatted": "00:01",
        "timestamp": 1,
        "text": text,
        "screenshot": "",
        "unified_analysis": {"severity": "high", "summary": "render hardening fixture."},
    }


# --- Frame MIME ------------------------------------------------------------


def test_html_frame_data_uri_declares_jpeg_for_jpg(tmp_path: Path) -> None:
    detection = _detection(1, 10.0)
    shot = tmp_path / "frame.jpg"
    shot.write_bytes(b"\xff\xd8\xff\xe0fake-jpeg-bytes")  # JPEG magic prefix

    out = tmp_path / "r.html"
    save_html_report_pro(
        detections=[detection],
        screenshots=[(detection, shot)],
        video_path=tmp_path / "v.mov",
        output_path=out,
    )
    html_text = out.read_text("utf-8")
    # Inspect the frame's own data URI (embedded findings JSON), not the whole
    # document — the vendored export JS legitimately mentions image/png.
    screenshot = _embedded_findings(html_text)[0]["screenshot"]
    assert screenshot.startswith("data:image/jpeg;base64,")
    assert "image/png" not in screenshot


# --- Slot-literal safety ---------------------------------------------------


def test_render_does_not_break_on_slot_literal_in_text() -> None:
    html = render_html_report_pro(
        video_name="clip.mp4",
        video_path=None,
        generated_at="2026-07-07T00:00:00Z",
        executive_summary="see {slot:head} inline",
        findings=[_finding_dict(text="look at {slot:main_panels} here")],
        language="en",
    )
    assert isinstance(html, str) and html.strip()
    # The literal is preserved as text, not consumed as a shell slot.
    assert "{slot:main_panels}" in html


# --- Video-path leak -------------------------------------------------------


def test_html_does_not_leak_absolute_video_path_when_missing() -> None:
    abs_video = "/home/someone/secret-project/demo.mov"
    html = render_html_report_pro(
        video_name="demo.mov",
        video_path=abs_video,
        generated_at="2026-07-07T00:00:00Z",
        executive_summary="summary",
        findings=[_finding_dict()],
        language="en",
    )
    assert abs_video not in html
    assert str(Path(abs_video).parent) not in html
    # Only the basename is exposed as the source reference.
    assert 'src="demo.mov"' in html
