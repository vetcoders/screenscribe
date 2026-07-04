"""Tests for the Markdown report endpoint (Group C, Issue #7).

The "Generate Markdown Report" button in the analyze dashboard previously
downloaded the same JSON payload as "Export JSON" - misleading. It now
returns a real Markdown report rendered by
``screenscribe.report.save_enhanced_markdown_report`` so the output is
consistent with the rest of the project (and AI-fixer-friendly).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from screenscribe.analyze_server import create_analyze_app
from screenscribe.config import ScreenScribeConfig

PNG_1X1_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


@pytest.fixture
def sample_video(tmp_path: Path) -> Path:
    video_path = tmp_path / "demo_video.mp4"
    video_path.write_bytes(b"\x00\x00\x00\x14ftypmp42\x00\x00\x00\x00mp42")
    return video_path


def _config() -> ScreenScribeConfig:
    return ScreenScribeConfig(
        api_key="test-key",  # pragma: allowlist secret
        vision_api_key="test-key",  # pragma: allowlist secret
        stt_endpoint="https://api.example.com/v1/audio/transcriptions",
        stt_model="test-model",
    )


def _mark(client: TestClient, *, timestamp: float, transcript: str = "", notes: str = "") -> str:
    response = client.post(
        "/api/mark",
        json={
            "timestamp": timestamp,
            "frame_base64": PNG_1X1_BASE64,
            "transcript": transcript,
            "notes": notes,
        },
    )
    assert response.status_code == 200
    return str(response.json()["marker_id"])


def test_markdown_report_empty_session(sample_video: Path) -> None:
    """No markers yet - report still renders with header + zero stats."""
    app = create_analyze_app(sample_video, _config())
    client = TestClient(app)

    response = client.get("/api/report/markdown")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    assert 'attachment; filename="analyze_report.md"' in response.headers.get(
        "content-disposition", ""
    )

    body = response.text
    assert "# Video Review Report" in body
    assert "demo_video.mp4" in body
    # Stats line is rendered even with zero detections.
    assert "0 findings" in body or "0 issues" in body


def test_markdown_report_includes_pending_markers(sample_video: Path) -> None:
    """Markers without an analysis result still appear without posing as issues.

    Without this guarantee, users would think Generate Report drops their
    pending findings (silent data loss). They also must not be promoted to
    actual medium-severity issues before VLM analysis runs.
    """
    app = create_analyze_app(sample_video, _config())
    client = TestClient(app)

    _mark(client, timestamp=12.5, transcript="this button is broken")

    response = client.get("/api/report/markdown")
    assert response.status_code == 200
    body = response.text
    # Section + raw transcript text appears in the body.
    assert "## User-Marked Moments (Pending Analysis)" in body
    assert "this button is broken" in body
    assert "## Transcript" in body
    assert "| 0 non-issues filtered | 1 pending user-marked" in body
    assert "filtered| 1 pending" not in body
    assert "### [MEDIUM]" not in body


def test_markdown_report_renders_analysis_results(
    sample_video: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Analyzed markers contribute summary + severity + suggested fix to the MD."""
    app = create_analyze_app(sample_video, _config())
    client = TestClient(app)

    marker_id = _mark(client, timestamp=8.0, transcript="login fails")

    def fake_analyze(detection: Any, screenshot_path: Path | None, **_: Any) -> Any:
        from screenscribe.unified_analysis import UnifiedFinding

        return UnifiedFinding(
            detection_id=0,
            screenshot_path=screenshot_path,
            timestamp=detection.segment.start,
            category="bug",
            is_issue=True,
            sentiment="problem",
            severity="high",
            summary="Login button does nothing on click",
            action_items=["Wire onClick handler"],
            affected_components=["LoginForm"],
            suggested_fix="Bind submit handler in LoginForm.tsx",
            ui_elements=["button"],
            issues_detected=["dead button"],
            accessibility_notes=[],
            design_feedback="",
            technical_observations="",
            response_id="resp-1",
        )

    monkeypatch.setattr(
        "screenscribe.unified_analysis.analyze_finding_unified",
        fake_analyze,
    )

    analyze_response = client.post(f"/api/analyze/{marker_id}")
    assert analyze_response.status_code == 200
    assert analyze_response.json()["status"] == "completed"

    response = client.get("/api/report/markdown")
    assert response.status_code == 200
    body = response.text
    assert "Login button does nothing on click" in body
    assert "Bind submit handler in LoginForm.tsx" in body
    assert "LoginForm" in body
    assert "[HIGH]" in body or "**Summary:** Login button does nothing on click" in body


def test_markdown_report_collapses_none_severity(
    sample_video: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A finding the reviewer kept but did not rank ('none') must not render a
    ``### [NONE]`` header (G2). It carries no severity tag -- mirroring the
    review card badge -- yet stays visible and is surfaced explicitly in the
    stats line as ``no-priority`` so the breakdown reconciles with the total
    instead of silently swallowing the finding.
    """
    app = create_analyze_app(sample_video, _config())
    client = TestClient(app)

    marker_id = _mark(client, timestamp=8.0, transcript="minor spacing nit")

    def fake_analyze(detection: Any, screenshot_path: Path | None, **_: Any) -> Any:
        from screenscribe.unified_analysis import UnifiedFinding

        return UnifiedFinding(
            detection_id=0,
            screenshot_path=screenshot_path,
            timestamp=detection.segment.start,
            category="ui",
            is_issue=True,
            sentiment="problem",
            severity="none",
            summary="Header padding is a touch tight but non-blocking",
            action_items=[],
            affected_components=[],
            suggested_fix="",
            ui_elements=[],
            issues_detected=[],
            accessibility_notes=[],
            design_feedback="",
            technical_observations="",
            response_id="resp-none",
        )

    monkeypatch.setattr(
        "screenscribe.unified_analysis.analyze_finding_unified",
        fake_analyze,
    )

    analyze_response = client.post(f"/api/analyze/{marker_id}")
    assert analyze_response.status_code == 200

    response = client.get("/api/report/markdown")
    assert response.status_code == 200
    body = response.text

    # The finding is present but tagless -- no leaked [NONE], and the summary
    # survives (no-priority is not no-finding).
    assert "[NONE]" not in body
    assert "Header padding is a touch tight but non-blocking" in body
    # Explicit, not silent: the stats breakdown names the no-priority finding.
    assert "no-priority" in body


def test_markdown_report_handles_empty_transcript(sample_video: Path) -> None:
    """Markers added with notes only (no transcript) must not crash MD render.

    Common case in analyze: silent screen recordings where the user types
    notes instead of speaking. The transcript section is omitted but the
    issue still appears with the notes text.
    """
    app = create_analyze_app(sample_video, _config())
    client = TestClient(app)

    _mark(client, timestamp=3.0, transcript="", notes="Visible glitch in header")

    response = client.get("/api/report/markdown")
    assert response.status_code == 200
    body = response.text
    # Transcript section absent.
    assert "## Transcript" not in body
    # But the marker still shows up via the "(user-marked frame, no transcript)"
    # placeholder OR the notes context.
    assert "user-marked frame" in body or "Visible glitch in header" in body


def test_markdown_report_does_not_break_after_marker_deletion(
    sample_video: Path,
) -> None:
    """Deleting all markers leaves the report at the empty-state baseline."""
    app = create_analyze_app(sample_video, _config())
    client = TestClient(app)

    marker_id = _mark(client, timestamp=2.0, transcript="quick check")
    client.delete(f"/api/marker/{marker_id}")

    response = client.get("/api/report/markdown")
    assert response.status_code == 200
    assert "# Video Review Report" in response.text


def test_dashboard_button_wires_markdown_endpoint(sample_video: Path) -> None:
    """The dashboard JS must call generateMarkdownReport (not the old JSON path).

    Group D renamed the visible button label from "Generate Markdown Report"
    to "Report MD" (Issue #10c) and added the Polish "Raport MD" via the
    i18n dict (Issue #9). Both labels must be present in the rendered HTML
    because the EN one ships as the default rendered text and the PL one
    lives in the inline i18n dictionary picked up by the toggle.
    """
    app = create_analyze_app(sample_video, _config())
    client = TestClient(app)

    response = client.get("/")
    assert response.status_code == 200
    html = response.text
    assert "generateMarkdownReport" in html
    assert "/api/report/markdown" in html
    assert "Report MD" in html
    assert "Pobierz raport" in html


def test_export_json_still_works_no_regression(sample_video: Path) -> None:
    """Issue #7's switch to MD must not break the JSON export endpoint."""
    app = create_analyze_app(sample_video, _config())
    client = TestClient(app)

    _mark(client, timestamp=1.0, transcript="hello")

    response = client.get("/api/export")
    assert response.status_code == 200
    payload = response.json()
    assert payload["video"].endswith("demo_video.mp4")
    assert len(payload["work_items"]) == 1
    assert payload["work_items"][0]["transcript"] == "hello"
