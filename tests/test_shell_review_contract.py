"""DOM-contract tests for the shell-composed REVIEW report."""

from __future__ import annotations

import json
import re

from screenscribe.html_pro.renderer import render_html_report_pro
from screenscribe.transcribe import Segment

PAYLOAD = "</script><img src=x onerror=alert(1)>"

_FINDINGS_ISLAND = re.compile(
    r'<script id="original-findings" type="application/json">\s*(.*?)\s*</script>',
    re.DOTALL,
)
_SEGMENTS_ISLAND = re.compile(r"window\.TRANSCRIPT_SEGMENTS = (\[.*?\]);")


def _render_review() -> str:
    return render_html_report_pro(
        video_name="contract-test.mov",
        video_path=None,
        generated_at="2026-06-13T00:00:00",
        executive_summary="",
        findings=[
            {
                "id": 1,
                "category": "bug",
                "timestamp": 3.5,
                "timestamp_formatted": "00:03",
                "text": f"User note {PAYLOAD}",
                "screenshot": "data:image/png;base64,AAAA",
                "unified_analysis": {
                    "is_issue": True,
                    "severity": "high",
                    "summary": PAYLOAD,
                    "suggested_fix": PAYLOAD,
                    "affected_components": ["Review panel"],
                    "issues_detected": ["Bad island"],
                    "action_items": ["Keep shell safe"],
                },
            }
        ],
        segments=[Segment(id=1, start=0.0, end=4.0, text=f"speaker: {PAYLOAD}")],
        errors=[],
        language="en",
    )


def _strip_scripts(html: str) -> str:
    """Drop inlined <script> bodies so DOM-only assertions are not confused by
    selector strings the bundled JS still references (e.g. ``.finding``)."""
    return re.sub(r"<script\b.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)


def _assert_ordered(html: str, needles: list[str]) -> None:
    positions = [html.index(needle) for needle in needles]
    assert positions == sorted(positions), list(zip(needles, positions, strict=True))


def test_review_shell_dom_contract() -> None:
    html = _render_review()

    assert 'data-surface-id="review"' in html
    assert 'data-i18n-namespace="review"' in html
    assert 'class="workspace-shell"' in html
    assert 'class="modal-layer"' in html

    for critical_id in (
        "detachReviewBtn",
        "attachWorkspaceBtn",
        "videoPlayer",
        "videoControls",
        "transcriptPanel",
        "subtitleSearch",
        "subtitleList",
        "sidebarResizer",
        "tab-summary",
        "tab-findings",
        "tab-export",
        "manualFindingsSection",
        "manualFindingsList",
        "manualFrameModal",
        "manualFramePreview",
        "manualFrameMicBtn",
        "manualFrameAnalyzeBtn",
        "manualFrameAddBtn",
        "lightbox",
        "lightbox-img",
        "lightbox-toolbar",
        "reviewer-name",
    ):
        assert f'id="{critical_id}"' in html

    anchors = set(re.findall(r'data-i18n="([^"]+)"', html))
    assert {
        "detachReview",
        "attachWorkspace",
        "playLabel",
        "captureFrame",
        "transcript",
        "manualFramesHeading",
        "review",
        "verdict",
        "yes",
        "noFalseAlarm",
        "changePriority",
        "notes",
        "voiceNote",
        "reviewer",
        "saveToDisk",
        "exportTodo",
        "exportJson",
        "exportZip",
        "manualFrameAnalysis",
        "manualFrameAnalyze",
        "manualFrameAdd",
        "toolPen",
        "toolDone",
    }.issubset(anchors)

    attr_anchors = set(re.findall(r'data-i18n-attr="([^"]+)"', html))
    assert "placeholder:searchTranscript" in attr_anchors

    for button_contract in (
        'data-action="voice-note"',
        # Post-C7.2: save/export buttons wired via data-action + event delegation.
        'data-action="save-review"',
        'data-action="export-todo"',
        'data-action="export-json"',
        'data-action="export-zip"',
        'data-action="close-manual-frame"',
        'data-tool="text"',
    ):
        assert button_contract in html

    # The Statistics tab is removed from the review UI (button + panel + cards).
    assert 'data-tab="stats"' not in html
    assert 'id="tab-stats"' not in html
    assert 'class="stat-card' not in _strip_scripts(html)
    assert "Statistics" not in _strip_scripts(html)

    # Artifact downloads (TODO/JSON/ZIP) live in the final Export tab only, not
    # in the always-visible sidebar footer. Save review stays in the footer.
    export_panel_idx = html.index('id="tab-export"')
    footer_idx = html.index('class="sidebar-footer"')
    for download in (
        'data-action="export-todo"',
        'data-action="export-json"',
        'data-action="export-zip"',
    ):
        assert export_panel_idx < html.index(download) < footer_idx, download
    assert footer_idx < html.index('data-action="save-review"')

    # The header "Momenty (N)" tab counter needs a span with a stable id so
    # review_app.js can add manual moments to the AI findings count live
    # (server only knows the AI count at render time).
    assert '<span id="findings-count">1</span>' in html

    assert 'name="verdict-1" value="accepted"' in html
    assert 'name="verdict-1" value="rejected"' in html
    assert 'class="severity-select"' in html
    assert 'class="reviewer-field"' in html
    assert 'id="reviewer-name"' in html
    assert '<textarea placeholder="Your notes, actions to take..."' in html

    _assert_ordered(
        html,
        [
            'id="original-findings"',
            "window.TRANSCRIPT_SEGMENTS",
            "window.I18N_BUNDLE",
            "attachLanguageControl",
            "attachSttTransport",
            "attachTabKeyboard",
            "class ScreenScribePlayer",
            "class ReviewVoiceRecorder",
        ],
    )


def test_review_shell_preserves_json_island_escaping() -> None:
    html = _render_review()

    findings = _FINDINGS_ISLAND.search(html)
    assert findings, "findings JSON island missing"
    assert "<" not in findings.group(1)
    parsed_findings = json.loads(findings.group(1))
    assert parsed_findings[0]["unified_analysis"]["summary"] == PAYLOAD

    segments = _SEGMENTS_ISLAND.search(html)
    assert segments, "TRANSCRIPT_SEGMENTS island missing"
    assert "<" not in segments.group(1)
    parsed_segments = json.loads(segments.group(1))
    assert parsed_segments[0]["text"] == f"speaker: {PAYLOAD}"
