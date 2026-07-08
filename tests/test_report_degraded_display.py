"""D7: low-confidence / degraded findings are shown as unverified, not vetted.

A finding with ``confidence == "degraded"`` OR
``parsed_from_unstructured_output == True`` (recovered from non-JSON model
prose) must carry a visible "unverified" marker across JSON, Markdown and HTML,
while an ordinary high-confidence finding carries no such marker. The marker is
presentational only — it never hides the finding, and it does not touch parsing.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from screenscribe.detect import Detection
from screenscribe.report import (
    save_enhanced_json_report,
    save_enhanced_markdown_report,
    save_html_report_pro,
)
from screenscribe.report.data import DEGRADED_MARKER_LABEL, DEGRADED_MARKER_LABEL_PL
from screenscribe.transcribe import Segment
from screenscribe.unified_analysis import UnifiedFinding


def _detection(
    seg_id: int, start: float, *, text: str = "the save button does nothing"
) -> Detection:
    return Detection(
        segment=Segment(id=seg_id, start=start, end=start + 2.0, text=text),
        category="bug",
        keywords_found=["semantic:bug"],
        context="context",
    )


def _finding(
    detection: Detection,
    *,
    confidence: str = "high",
    parsed_from_unstructured_output: bool = False,
    summary: str = "Save button has no handler",
) -> UnifiedFinding:
    return UnifiedFinding(
        detection_id=detection.segment.id,
        screenshot_path=None,
        timestamp=detection.segment.start,
        category=detection.category,
        is_issue=True,
        sentiment="problem",
        severity="high",
        summary=summary,
        action_items=["Wire up the handler"],
        affected_components=["SaveButton"],
        suggested_fix="Attach onClick",
        ui_elements=["button"],
        issues_detected=["no handler"],
        accessibility_notes=[],
        design_feedback="",
        technical_observations="",
        response_id="resp_1",
        confidence=confidence,
        parsed_from_unstructured_output=parsed_from_unstructured_output,
    )


def _png(path: Path) -> Path:
    path.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    return path


def _embedded_findings(html_text: str) -> list[dict]:
    match = re.search(r'<script id="original-findings"[^>]*>(.*?)</script>', html_text, re.DOTALL)
    assert match, "HTML report must embed original-findings JSON"
    return json.loads(match.group(1))


# --- JSON ------------------------------------------------------------------


def test_json_marks_degraded_finding(tmp_path: Path) -> None:
    detection = _detection(1, 10.0)
    shot = _png(tmp_path / "shot.png")
    finding = _finding(detection, confidence="degraded")

    out = tmp_path / "r.json"
    save_enhanced_json_report(
        detections=[detection],
        screenshots=[(detection, shot)],
        video_path=tmp_path / "v.mov",
        output_path=out,
        unified_findings=[finding],
    )
    ua = json.loads(out.read_text("utf-8"))["findings"][0]["unified_analysis"]
    assert ua["degraded"] is True
    assert ua["confidence"] == "degraded"


def test_json_marks_unstructured_parsed_finding(tmp_path: Path) -> None:
    """A high-confidence label is still degraded when recovered from prose."""
    detection = _detection(1, 10.0)
    shot = _png(tmp_path / "shot.png")
    finding = _finding(detection, confidence="high", parsed_from_unstructured_output=True)

    out = tmp_path / "r.json"
    save_enhanced_json_report(
        detections=[detection],
        screenshots=[(detection, shot)],
        video_path=tmp_path / "v.mov",
        output_path=out,
        unified_findings=[finding],
    )
    ua = json.loads(out.read_text("utf-8"))["findings"][0]["unified_analysis"]
    assert ua["degraded"] is True
    assert ua["parsed_from_unstructured_output"] is True


def test_json_normal_finding_not_degraded(tmp_path: Path) -> None:
    detection = _detection(1, 10.0)
    shot = _png(tmp_path / "shot.png")

    out = tmp_path / "r.json"
    save_enhanced_json_report(
        detections=[detection],
        screenshots=[(detection, shot)],
        video_path=tmp_path / "v.mov",
        output_path=out,
        unified_findings=[_finding(detection)],
    )
    ua = json.loads(out.read_text("utf-8"))["findings"][0]["unified_analysis"]
    assert ua["degraded"] is False
    assert ua["confidence"] == "high"


# --- Markdown --------------------------------------------------------------


def test_markdown_marks_degraded_finding(tmp_path: Path) -> None:
    detection = _detection(1, 10.0)
    shot = _png(tmp_path / "shot.png")

    out = tmp_path / "r.md"
    save_enhanced_markdown_report(
        detections=[detection],
        screenshots=[(detection, shot)],
        video_path=tmp_path / "v.mov",
        output_path=out,
        unified_findings=[_finding(detection, confidence="degraded")],
    )
    text = out.read_text("utf-8")
    assert DEGRADED_MARKER_LABEL in text
    # The finding itself is still present (not hidden).
    assert "Save button has no handler" in text


def test_markdown_normal_finding_no_marker(tmp_path: Path) -> None:
    detection = _detection(1, 10.0)
    shot = _png(tmp_path / "shot.png")

    out = tmp_path / "r.md"
    save_enhanced_markdown_report(
        detections=[detection],
        screenshots=[(detection, shot)],
        video_path=tmp_path / "v.mov",
        output_path=out,
        unified_findings=[_finding(detection)],
    )
    text = out.read_text("utf-8")
    assert DEGRADED_MARKER_LABEL not in text
    assert "Save button has no handler" in text


# --- HTML ------------------------------------------------------------------


def test_html_marks_degraded_finding_visibly(tmp_path: Path) -> None:
    detection = _detection(1, 10.0)
    shot = _png(tmp_path / "shot.png")

    out = tmp_path / "r.html"
    save_html_report_pro(
        detections=[detection],
        screenshots=[(detection, shot)],
        video_path=tmp_path / "v.mov",
        output_path=out,
        unified_findings=[_finding(detection, parsed_from_unstructured_output=True)],
    )
    html_text = out.read_text("utf-8")
    # Visible marker rendered into the summary the Pro template displays.
    assert DEGRADED_MARKER_LABEL in html_text
    # Machine-readable flag survives in the embedded findings JSON.
    findings = _embedded_findings(html_text)
    assert findings[0]["unified_analysis"]["degraded"] is True


def test_html_degraded_marker_is_localized_pl(tmp_path: Path) -> None:
    """A PL report shows the PL marker, never the raw EN chrome string."""
    detection = _detection(1, 10.0)
    shot = _png(tmp_path / "shot.png")

    out = tmp_path / "r.html"
    save_html_report_pro(
        detections=[detection],
        screenshots=[(detection, shot)],
        video_path=tmp_path / "v.mov",
        output_path=out,
        unified_findings=[_finding(detection, confidence="degraded")],
        language="pl",
    )
    html_text = out.read_text("utf-8")
    assert DEGRADED_MARKER_LABEL_PL in html_text
    # The English chrome string must not leak into the localized summary.
    assert DEGRADED_MARKER_LABEL not in html_text
    findings = _embedded_findings(html_text)
    assert findings[0]["unified_analysis"]["degraded"] is True


def test_html_normal_finding_no_marker(tmp_path: Path) -> None:
    detection = _detection(1, 10.0)
    shot = _png(tmp_path / "shot.png")

    out = tmp_path / "r.html"
    save_html_report_pro(
        detections=[detection],
        screenshots=[(detection, shot)],
        video_path=tmp_path / "v.mov",
        output_path=out,
        unified_findings=[_finding(detection)],
    )
    html_text = out.read_text("utf-8")
    assert DEGRADED_MARKER_LABEL not in html_text
    findings = _embedded_findings(html_text)
    assert findings[0]["unified_analysis"]["degraded"] is False
