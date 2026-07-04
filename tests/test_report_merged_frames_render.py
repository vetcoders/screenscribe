"""Merged-away evidence frames must render under their survivor (P2, finding #4).

The report writers (``screenscribe/report/{json,html}_report.py``) populate a
``merged_frames`` list on each survivor: the screenshots + transcript of the
findings auto-folded into it. The HTML Pro renderer only printed a ``merged
from N`` COUNTER from ``merged_from_ids`` and never read ``merged_frames``, so
an auto-merged finding silently dropped every absorbed frame's evidence
(screenshot + spoken transcript) from the report UI.

These tests assert the rendered finding carries the member frames' actual
evidence (transcript text + a screenshot reference), not merely a count.
"""

from __future__ import annotations

import re

from screenscribe.html_pro.renderer import render_html_report_pro


def _strip_embedded_payloads(html: str) -> str:
    """Drop <script>/<style> bodies so substring checks see RENDERED DOM only.

    The full findings list (with merged_frames) is embedded as JSON in a
    <script id="original-findings"> blob for the review runtime, so a naive
    substring check would pass even if nothing were rendered. Stripping the
    payloads isolates the visible report markup.
    """
    html = re.sub(r"<script\b.*?</script>", "", html, flags=re.IGNORECASE | re.DOTALL)
    return re.sub(r"<style\b.*?</style>", "", html, flags=re.IGNORECASE | re.DOTALL)


def _finding_with_merged_frames() -> dict:
    return {
        "id": 1,
        "category": "bug",
        "timestamp": 5.0,
        "timestamp_formatted": "00:05",
        "text": "survivor transcript line",
        "screenshot": "data:image/png;base64,QUJD",
        "unified_analysis": {
            "is_issue": True,
            "severity": "high",
            "summary": "survivor summary",
            "merged_from_ids": [2, 3],
        },
        "merged_frames": [
            {
                "id": 2,
                "timestamp": 7.0,
                "timestamp_formatted": "00:07",
                "text": "ABSORBED MEMBER TWO TRANSCRIPT",
                "screenshot_path": "frame_000002.png",
            },
            {
                "id": 3,
                "timestamp": 9.0,
                "timestamp_formatted": "00:09",
                "text": "ABSORBED MEMBER THREE TRANSCRIPT",
                "screenshot_path": "frame_000003.png",
            },
        ],
    }


def _render(language: str = "en") -> str:
    return render_html_report_pro(
        video_name="merge.mov",
        video_path=None,
        generated_at="2026-06-28T00:00:00",
        executive_summary="",
        findings=[_finding_with_merged_frames()],
        segments=[],
        errors=[],
        language=language,
    )


def test_merged_frames_member_transcript_is_rendered() -> None:
    """Each absorbed member's transcript text reaches the RENDERED HTML (not just a count)."""
    rendered = _strip_embedded_payloads(_render())
    assert "finding-merged-frames" in rendered, "merged-evidence block missing"
    assert "ABSORBED MEMBER TWO TRANSCRIPT" in rendered
    assert "ABSORBED MEMBER THREE TRANSCRIPT" in rendered


def test_merged_frames_member_screenshot_reference_is_rendered() -> None:
    """Each absorbed member's screenshot is referenced as evidence under the survivor."""
    rendered = _strip_embedded_payloads(_render())
    assert "frame_000002.png" in rendered
    assert "frame_000003.png" in rendered


def test_unmerged_finding_has_no_merged_evidence_block() -> None:
    """A finding with no merged_frames must not emit the evidence block."""
    plain = {
        "id": 9,
        "category": "ui",
        "timestamp": 1.0,
        "timestamp_formatted": "00:01",
        "text": "lonely finding",
        "screenshot": "",
        "unified_analysis": {"is_issue": True, "severity": "low", "summary": "s"},
    }
    html = render_html_report_pro(
        video_name="merge.mov",
        video_path=None,
        generated_at="2026-06-28T00:00:00",
        executive_summary="",
        findings=[plain],
        segments=[],
        errors=[],
        language="en",
    )
    assert "finding-merged-frames" not in html
