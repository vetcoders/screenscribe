"""Test TODO export functionality in HTML Pro reports."""

from typing import Any

from screenscribe.html_pro import render_html_report_pro
from screenscribe.transcribe import Segment


def create_minimal_finding() -> dict[str, Any]:
    """Create a minimal finding for testing."""
    return {
        "id": 1,
        "category": "bug",
        "timestamp": "00:00",
        "timestamp_seconds": 0.0,
        "timestamp_formatted": "00:00",
        "text": "Test issue",
        "context": "Test context",
        "keywords": ["test"],
        "screenshot_b64": "",
        "thumbnail_b64": "",
        "is_issue": True,
        "severity": "high",
        "summary": "Test summary",
        "action_items": ["Fix this"],
        "affected_components": [],
        "suggested_fix": "Do something",
        "ui_elements": [],
        "unified_analysis": {
            "summary": "Test unified summary",
            "severity": "high",
            "action_items": ["Action 1", "Action 2"],
        },
    }


class TestTodoExport:
    """Tests for TODO markdown export functionality."""

    def test_todo_checkbox_is_unchecked_by_default(self) -> None:
        """Exported TODO items should have unchecked [ ] checkboxes, not [x].

        Confirmed issues are tasks TO DO, not completed tasks.
        Regression test for: dd750f5 fix: TODO items should be unchecked by default
        """
        segments = [Segment(id=1, start=0.0, end=5.0, text="Test segment")]
        findings = [create_minimal_finding()]

        html_content = render_html_report_pro(
            video_name="test.mov",
            video_path=None,
            generated_at="2026-01-05",
            executive_summary="Test summary",
            findings=findings,
            segments=segments,
        )

        # The JavaScript in HTML should use unchecked checkbox for TODOs
        assert "const checkbox = '[ ]';" in html_content, (
            "TODO export should use unchecked [ ] checkboxes, not [x]"
        )
        # Should NOT contain the old buggy version
        assert "const checkbox = verdict === 'accepted' ? '[x]'" not in html_content, (
            "Old buggy checkbox logic should not be present"
        )
