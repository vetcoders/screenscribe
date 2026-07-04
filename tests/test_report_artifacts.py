"""Regression tests for report artifact completeness and review UI wiring."""

import json
import re
from pathlib import Path

from screenscribe.checkpoint import (
    deserialize_unified_finding,
    serialize_unified_finding,
)
from screenscribe.detect import Detection
from screenscribe.html_pro.renderer import render_html_report_pro
from screenscribe.report import (
    save_enhanced_json_report,
    save_enhanced_markdown_report,
    save_html_report_pro,
)
from screenscribe.transcribe import Segment
from screenscribe.unified_analysis import UnifiedFinding


def _sample_detection() -> Detection:
    return Detection(
        segment=Segment(id=1, start=12.5, end=15.0, text="Przycisk dalej nie działa poprawnie."),
        category="bug",
        keywords_found=["semantic:bug"],
        context="Użytkownik raportuje problem z przyciskiem na ekranie konfiguracji.",
    )


def _sample_segments() -> list[Segment]:
    return [
        Segment(id=1, start=12.5, end=15.0, text="Przycisk dalej nie działa poprawnie."),
        Segment(id=2, start=15.2, end=18.1, text="Kliknięcie nic nie robi i brak informacji."),
    ]


def _sample_unified_finding(
    detection: Detection,
    *,
    summary: str = "Problem z CTA",
    severity: str = "high",
) -> UnifiedFinding:
    return UnifiedFinding(
        detection_id=detection.segment.id,
        screenshot_path=None,
        timestamp=detection.segment.start,
        category=detection.category,
        is_issue=True,
        sentiment="problem",
        severity=severity,
        summary=summary,
        action_items=["Naprawić handler kliknięcia"],
        affected_components=["CTA button"],
        suggested_fix="Sprawdzić event listener",
        ui_elements=[],
        issues_detected=[],
        accessibility_notes=[],
        design_feedback="",
        technical_observations="",
        response_id="resp_test",
    )


def test_enhanced_json_report_persists_timestamped_transcript(tmp_path: Path) -> None:
    detection = _sample_detection()
    screenshot = tmp_path / "shot.jpg"
    screenshot.write_bytes(b"fake")
    output = tmp_path / "report.json"

    save_enhanced_json_report(
        detections=[detection],
        screenshots=[(detection, screenshot)],
        video_path=tmp_path / "video.mov",
        output_path=output,
        transcript="Przycisk dalej nie działa poprawnie. Kliknięcie nic nie robi.",
        transcript_segments=_sample_segments(),
    )

    data = output.read_text(encoding="utf-8")
    assert '"transcript_timestamped"' in data
    assert "[12.5s - 15.0s] Przycisk dalej nie działa poprawnie." in data
    assert '"transcript_segments"' in data


def test_enhanced_markdown_report_includes_timestamped_transcript(tmp_path: Path) -> None:
    detection = _sample_detection()
    screenshot = tmp_path / "shot.jpg"
    screenshot.write_bytes(b"fake")
    output = tmp_path / "report.md"

    save_enhanced_markdown_report(
        detections=[detection],
        screenshots=[(detection, screenshot)],
        video_path=tmp_path / "video.mov",
        output_path=output,
        transcript="Przycisk dalej nie działa poprawnie. Kliknięcie nic nie robi.",
        transcript_segments=_sample_segments(),
    )

    md = output.read_text(encoding="utf-8")
    assert "## Timestamped Transcript" in md
    assert "[12.5s - 15.0s] Przycisk dalej nie działa poprawnie." in md


def test_enhanced_json_report_does_not_leak_absolute_paths(tmp_path: Path) -> None:
    """Shareable JSON must carry only basenames for BOTH the source video and the
    screenshots — never absolute local paths (privacy: no /Users/... or nested
    frame-dir fingerprint in shared artifacts)."""
    detection = _sample_detection()
    frames_dir = tmp_path / "home" / "someone" / "frames"
    frames_dir.mkdir(parents=True)
    screenshot = frames_dir / "frame_001.png"
    screenshot.write_bytes(b"fake")
    output = tmp_path / "report.json"
    abs_video = (tmp_path / "home" / "someone" / "demo.mov").resolve()

    save_enhanced_json_report(
        detections=[detection],
        screenshots=[(detection, screenshot)],
        video_path=abs_video,
        output_path=output,
        transcript="x",
        transcript_segments=_sample_segments(),
    )

    raw = output.read_text(encoding="utf-8")
    data = json.loads(raw)
    # video → basename only
    assert data["video"] == "demo.mov"
    assert "/" not in data["video"]
    # screenshot → basename only, never the absolute path or its parent dir
    assert "frame_001.png" in raw
    assert str(screenshot) not in raw
    assert str(screenshot.parent) not in raw
    assert str(abs_video) not in raw
    assert str(abs_video.parent) not in raw


def test_enhanced_json_report_counts_fallback_findings_when_unified_is_empty(
    tmp_path: Path,
) -> None:
    detection = _sample_detection()
    screenshot = tmp_path / "shot.jpg"
    screenshot.write_bytes(b"fake")
    output = tmp_path / "report.json"

    save_enhanced_json_report(
        detections=[detection],
        screenshots=[(detection, screenshot)],
        video_path=tmp_path / "video.mov",
        output_path=output,
        unified_findings=[],
        errors=[
            {
                "stage": "unified_analysis",
                "message": "1 of 1 unified analyses failed. The report fell back to transcript/screenshot-only findings for those items.",
            }
        ],
    )

    data = output.read_text(encoding="utf-8")
    assert '"total": 1' in data
    assert '"bugs": 1' in data
    assert '"changes": 0' in data
    assert '"ui": 0' in data


def test_enhanced_json_report_keeps_all_findings_when_unified_is_partial(tmp_path: Path) -> None:
    first_detection = _sample_detection()
    second_detection = Detection(
        segment=Segment(id=2, start=21.0, end=24.0, text="Pole email nachodzi na etykietę."),
        category="ui",
        keywords_found=["semantic:ui"],
        context="Układ formularza rozjeżdża się na węższym widoku.",
    )
    first_screenshot = tmp_path / "shot-1.jpg"
    second_screenshot = tmp_path / "shot-2.jpg"
    first_screenshot.write_bytes(b"fake")
    second_screenshot.write_bytes(b"fake")
    output = tmp_path / "report.json"

    save_enhanced_json_report(
        detections=[first_detection, second_detection],
        screenshots=[
            (first_detection, first_screenshot),
            (second_detection, second_screenshot),
        ],
        video_path=tmp_path / "video.mov",
        output_path=output,
        unified_findings=[_sample_unified_finding(first_detection)],
    )

    report = json.loads(output.read_text(encoding="utf-8"))

    assert report["summary"]["total"] == 2
    assert report["summary"]["bugs"] == 1
    assert report["summary"]["ui"] == 1
    assert report["analysis_passes"]["unified_analysis"]["status"] == "partial"
    assert len(report["findings"]) == 2
    assert report["findings"][0]["unified_analysis"]["status"] == "completed"
    assert report["findings"][1]["unified_analysis"]["status"] == "missing"


def test_html_pro_report_contains_precision_controls_and_voice_note_action() -> None:
    findings = [
        {
            "id": 1,
            "category": "bug",
            "timestamp_formatted": "00:12",
            "timestamp": 12.5,
            "text": "Przycisk dalej nie działa poprawnie.",
            "context": "Kontekst testowy",
            "keywords": ["semantic:bug"],
            "screenshot": "",
            "screenshot_path": "",
            "unified_analysis": {
                "is_issue": True,
                "severity": "high",
                "summary": "Problem z CTA",
                "action_items": ["Naprawić handler kliknięcia"],
                "affected_components": ["CTA button"],
                "suggested_fix": "Sprawdzić event listener",
                "ui_elements": [],
                "issues_detected": [],
                "accessibility_notes": [],
                "design_feedback": "",
            },
        }
    ]
    html = render_html_report_pro(
        video_name="test.mov",
        video_path=None,
        generated_at="2026-02-15T17:31:26",
        executive_summary="",
        findings=findings,
        segments=_sample_segments(),
        errors=[],
    )

    assert 'controls preload="metadata"' in html
    assert 'id="secondBackBtn"' in html
    assert 'id="captureFrameBtn"' in html
    assert 'id="stepBackBtn"' in html
    assert 'id="manualFrameModal"' in html
    assert 'id="manualFindingsList"' in html
    assert 'data-tool="text"' in html
    assert 'data-action="voice-note"' in html
    assert 'class="notes-mic-btn"' in html
    assert 'class="workspace-shell"' in html
    assert 'id="transcriptPanel"' in html
    assert 'id="sidebarResizer"' in html
    assert 'class="sidebar-footer"' in html
    assert 'data-window-mode="workspace"' in html
    assert 'id="detachReviewBtn"' in html
    assert 'id="attachWorkspaceBtn"' in html
    assert "seek-to-timestamp" in html
    assert "handleIncomingWindowCommand" in html
    assert "screenscribe_ui_" in html
    assert "screenscribe_lang_" in html
    assert "__screenscribeAllowProgrammaticClose" in html
    assert "unsavedChangesWarning" in html
    assert "activateTab(" in html
    assert "focusReview" in html
    assert "Focus Review Window" in html
    assert "thumbnailTool.refreshFromState();" in html
    assert "annotationTools.get(findingId)?.refreshFromState();" in html
    assert "window.player = player;" in html
    assert 'data-i18n="findingSummary"' in html
    assert 'data-i18n="affectedComponents"' in html
    assert 'data-i18n="suggestedFix"' in html
    assert '"findingSummary": "Podsumowanie:"' in html
    assert 'id="currentSubtitle"' not in html
    assert 'id="transcriptDrawer"' not in html

    english_html = render_html_report_pro(
        video_name="test.mov",
        video_path=None,
        generated_at="2026-02-15T17:31:26",
        executive_summary="",
        findings=findings,
        segments=_sample_segments(),
        errors=[],
        language="en-US",
    )

    assert '<strong data-i18n="findingSummary">Summary:</strong>' in english_html
    assert '<dt data-i18n="affectedComponents">Affected Components</dt>' in english_html
    assert '<dt data-i18n="suggestedFix">Suggested Fix</dt>' in english_html
    assert '"findingSummary": "Summary:"' in english_html
    assert "<strong>Podsumowanie:</strong>" not in english_html
    assert "<dt>Powiązane komponenty</dt>" not in english_html


def test_html_pro_report_uses_requested_subtitle_language() -> None:
    html = render_html_report_pro(
        video_name="test.mov",
        video_path=None,
        generated_at="2026-02-15T17:31:26",
        executive_summary="",
        findings=[],
        segments=_sample_segments(),
        errors=[],
        language="en",
    )

    assert '<html lang="en">' in html
    assert 'data-report-language="en"' in html
    assert 'srclang="en"' in html
    assert 'label="English"' in html
    assert 'srclang="pl"' not in html


def test_html_pro_report_normalizes_regional_locale_for_ui_language() -> None:
    html = render_html_report_pro(
        video_name="test.mov",
        video_path=None,
        generated_at="2026-02-15T17:31:26",
        executive_summary="",
        findings=[],
        segments=_sample_segments(),
        errors=[],
        language="en-US",
    )

    assert '<html lang="en-us">' in html
    assert 'data-report-language="en"' in html
    assert "Your browser does not support HTML5 video." in html
    assert 'data-report-language="en"' in html
    assert "const primaryLanguage = normalizedLanguage.split('-', 1)[0];" in html
    assert 'srclang="en-US"' in html
    assert 'label="English"' in html


def test_html_pro_report_uses_relative_video_source_without_file_scheme(tmp_path: Path) -> None:
    detection = _sample_detection()
    screenshot = tmp_path / "shot.jpg"
    screenshot.write_bytes(b"fake")
    video = tmp_path / "source" / "sample.mov"
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"fake-video")
    output = tmp_path / "out" / "sample_report.html"
    output.parent.mkdir(parents=True, exist_ok=True)

    save_html_report_pro(
        detections=[detection],
        screenshots=[(detection, screenshot)],
        video_path=video,
        output_path=output,
        segments=_sample_segments(),
    )

    html = output.read_text(encoding="utf-8")
    assert 'src="sample.mov"' in html
    assert "file://" not in html
    assert (output.parent / "sample.mov").exists()


def test_html_pro_report_surfaces_pipeline_errors_when_ai_summary_missing() -> None:
    findings = [
        {
            "id": 1,
            "category": "bug",
            "timestamp_formatted": "00:12",
            "timestamp": 12.5,
            "text": "Przycisk dalej nie działa poprawnie.",
            "context": "Kontekst testowy",
            "keywords": ["semantic:bug"],
            "screenshot": "",
            "screenshot_path": "",
        }
    ]

    html = render_html_report_pro(
        video_name="test.mov",
        video_path=None,
        generated_at="2026-02-15T17:31:26",
        executive_summary="",
        findings=findings,
        segments=_sample_segments(),
        errors=[
            {
                "stage": "unified_analysis",
                "message": "7 of 7 unified analyses failed. The report fell back to transcript/screenshot-only findings for those items.",
            }
        ],
    )

    assert "Pipeline Errors" in html
    assert "7 of 7 unified analyses failed" in html
    assert "No AI summary available" in html
    assert "This report still contains 1 detected findings" in html


def test_unified_finding_checkpoint_roundtrip_preserves_reliability_fields() -> None:
    detection = _sample_detection()
    finding = _sample_unified_finding(detection)
    finding.confidence = "low"
    finding.parsed_from_unstructured_output = True

    payload = serialize_unified_finding(finding)
    assert payload["confidence"] == "low"
    assert payload["parsed_from_unstructured_output"] is True

    restored = deserialize_unified_finding(payload)
    assert restored.confidence == "low"
    assert restored.parsed_from_unstructured_output is True


def test_unified_finding_checkpoint_migration_applies_defaults_for_legacy_payloads() -> None:
    detection = _sample_detection()
    finding = _sample_unified_finding(detection)
    legacy_payload = serialize_unified_finding(finding)
    # Legacy checkpoints (pre-reliability-tracking) lack these two keys.
    legacy_payload.pop("confidence")
    legacy_payload.pop("parsed_from_unstructured_output")

    restored = deserialize_unified_finding(legacy_payload)

    assert restored.confidence == "high"
    assert restored.parsed_from_unstructured_output is False


def test_html_pro_review_uses_export_tab_and_drops_statistics_tab() -> None:
    """The Statistics tab is removed; its stat-cards no longer render. Findings
    still carry data-severity (client-side state keys off it), review-state
    hydration is wired, and the artifact downloads moved into the final Export
    tab rather than an always-visible footer."""
    html = render_html_report_pro(
        video_name="test.mov",
        video_path=None,
        generated_at="2026-02-15T17:31:26",
        executive_summary="",
        findings=[
            {
                "id": 1,
                "category": "bug",
                "timestamp_formatted": "00:12",
                "timestamp": 12.5,
                "text": "x",
                "unified_analysis": {"is_issue": True, "severity": "high", "summary": "s"},
            }
        ],
        segments=_sample_segments(),
        errors=[],
    )
    dom = re.sub(r"<script\b.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)

    # Statistics tab + stat-cards are gone from the DOM.
    assert 'id="tab-stats"' not in dom
    assert 'data-tab="stats"' not in dom
    assert 'class="stat-card' not in dom

    # Findings still carry severity for client-side state.
    assert 'data-severity="high"' in dom

    # Final Export tab hosts the artifact downloads; review-state stays wired.
    assert 'id="tab-export"' in dom
    export_idx = dom.index('id="tab-export"')
    # Post-C7.2 the artifact download is wired via data-action (no inline onclick).
    assert dom.index('data-action="export-zip"') > export_idx
    assert "/api/review-state" in html
    assert "hydrateReportStateFromDisk" in html
