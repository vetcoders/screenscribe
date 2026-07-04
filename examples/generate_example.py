#!/usr/bin/env python3
"""Generate the neutral, standalone example artifacts shipped in ``examples/``.

Deterministic and offline: no API key, no network, no video. It feeds neutral,
fictional English data through the SAME product code paths that a real run uses
(``render_html_report_pro`` and ``generate_webvtt``) so the example report stays
faithful to the actual output — and re-generates byte-identically (the
``generated_at`` timestamp is pinned).

Run from the repo root:

    uv run python examples/generate_example.py

The data here is a placeholder demo (a fictional "Acme Notes" web app review).
Content/brand sign-off of the shipped sample is an operator decision; this script
just provides a reproducible, PII-free default to iterate on.
"""

from __future__ import annotations

import json
from pathlib import Path

from screenscribe.html_pro.renderer import render_html_report_pro
from screenscribe.transcribe import Segment
from screenscribe.vtt_generator import generate_webvtt

# Pinned so re-running produces an identical diff (determinism gate).
GENERATED_AT = "2026-01-01T00:00:00"
VIDEO_NAME = "acme-notes-demo.mov"
LANGUAGE = "en"

EXECUTIVE_SUMMARY = (
    "Walkthrough of the Acme Notes demo web app. The reviewer narrates three "
    "actionable items while clicking through the note editor: a save button that "
    "reports success without persisting, a low-contrast toolbar that fails "
    "accessibility, and a layout that overflows on a narrow viewport. This is a "
    "neutral, fictional example with no real data."
)

# Neutral transcript (drives both the VTT track and subtitle sync in the HTML).
SEGMENTS = [
    Segment(id=1, start=0.0, end=4.0, text="Here is the Acme Notes demo. Let me open the editor."),
    Segment(
        id=2,
        start=4.0,
        end=9.5,
        text="I type a note and click Save. It says saved, but nothing persists on reload.",
    ),
    Segment(
        id=3,
        start=9.5,
        end=15.0,
        text="The toolbar icons are very light gray on white. Hard to read.",
    ),
    Segment(
        id=4,
        start=15.0,
        end=20.0,
        text="On a narrow window the sidebar overflows and covers the text.",
    ),
    Segment(
        id=5, start=20.0, end=24.0, text="That is the end of the walkthrough. Three items to fix."
    ),
]

# Findings carry the SAME dict shape the real pipeline hands to the renderer
# (see screenscribe/report/html_report.py + report/data.py::_serialize_unified_analysis):
#  - identity/locator fields live at the top level,
#  - ``timestamp`` is NUMERIC seconds (the renderer's JS seek target), and
#  - every AI field is nested under ``unified_analysis`` (where the renderer reads
#    severity / summary / action_items / suggested_fix / components / issues).
# Putting AI fields at the top level or storing ``timestamp`` as a display string
# makes the renderer fall back to severity "none", drop the summary/actions, and
# coerce the seek target to 0.0 — i.e. a broken advertisement of the product.
FINDINGS = [
    {
        "id": 1,
        "category": "bug",
        "timestamp": 4.0,  # numeric seconds — drives the JS seek target
        "timestamp_formatted": "00:04",
        "text": "I type a note and click Save. It says saved, but nothing persists on reload.",
        "context": "Note editor save flow",
        "keywords": ["save", "persistence"],
        "screenshot": "",
        "screenshot_path": "",
        "merged_frames": [],
        "unified_analysis": {
            "status": "completed",
            "is_issue": True,
            "degraded": False,
            "confidence": "high",
            "parsed_from_unstructured_output": False,
            "sentiment": "problem",
            "severity": "high",
            "summary": "Save reports success but the note is not persisted across reload.",
            "action_items": [
                "Confirm the write reaches the backend (network tab shows the request).",
                "Surface a real error state when the write fails instead of a false success.",
            ],
            "affected_components": ["NoteEditor", "SaveController"],
            "suggested_fix": "Await the persistence call and only show 'Saved' on a 2xx response.",
            "ui_elements": ["Save button", "Toast"],
            "issues_detected": ["False success toast", "Lost note on reload"],
            "accessibility_notes": [],
            "design_feedback": "",
            "technical_observations": "",
            "response_id": None,
            "merged_from_ids": [],
        },
    },
    {
        "id": 2,
        "category": "ui",
        "timestamp": 9.5,
        "timestamp_formatted": "00:09",
        "text": "The toolbar icons are very light gray on white. Hard to read.",
        "context": "Editor toolbar",
        "keywords": ["contrast", "accessibility"],
        "screenshot": "",
        "screenshot_path": "",
        "merged_frames": [],
        "unified_analysis": {
            "status": "completed",
            "is_issue": True,
            "degraded": False,
            "confidence": "high",
            "parsed_from_unstructured_output": False,
            "sentiment": "problem",
            "severity": "medium",
            "summary": "Toolbar icon contrast is below the WCAG AA threshold.",
            "action_items": [
                "Raise icon color contrast to at least 4.5:1 against the toolbar background.",
            ],
            "affected_components": ["EditorToolbar"],
            "suggested_fix": "Use a darker neutral for icon strokes and add a visible focus ring.",
            "ui_elements": ["Toolbar", "Icon buttons"],
            "issues_detected": ["Low contrast"],
            "accessibility_notes": ["Icons are not distinguishable for low-vision users."],
            "design_feedback": "Aim for a clear, high-contrast monochrome icon set.",
            "technical_observations": "",
            "response_id": None,
            "merged_from_ids": [],
        },
    },
    {
        "id": 3,
        "category": "change",
        "timestamp": 15.0,
        "timestamp_formatted": "00:15",
        "text": "On a narrow window the sidebar overflows and covers the text.",
        "context": "Responsive layout",
        "keywords": ["layout", "responsive"],
        "screenshot": "",
        "screenshot_path": "",
        "merged_frames": [],
        "unified_analysis": {
            "status": "completed",
            "is_issue": True,
            "degraded": False,
            "confidence": "high",
            "parsed_from_unstructured_output": False,
            "sentiment": "problem",
            "severity": "low",
            "summary": "Sidebar overlaps the editor at narrow viewport widths.",
            "action_items": [
                "Collapse the sidebar below a breakpoint or make it a toggling drawer.",
            ],
            "affected_components": ["AppShell", "Sidebar"],
            "suggested_fix": "Add a responsive breakpoint so the sidebar stacks instead of overlapping.",
            "ui_elements": ["Sidebar", "Editor pane"],
            "issues_detected": ["Overlap at narrow width"],
            "accessibility_notes": [],
            "design_feedback": "Keep the monochrome layout calm; avoid overlap at any width.",
            "technical_observations": "",
            "response_id": None,
            "merged_from_ids": [],
        },
    },
]

OUT_DIR = Path(__file__).resolve().parent


def _build_json() -> dict:
    """Build the report JSON matching report/json_report.py's schema."""
    summary = {
        "total": len(FINDINGS),
        "bugs": sum(1 for f in FINDINGS if f["category"] == "bug"),
        "changes": sum(1 for f in FINDINGS if f["category"] == "change"),
        "ui": sum(1 for f in FINDINGS if f["category"] == "ui"),
    }
    findings = [
        {
            "id": f["id"],
            "category": f["category"],
            "timestamp_start": f["timestamp"],
            "timestamp_end": f["timestamp"],
            "timestamp_formatted": f["timestamp_formatted"],
            "text": f["text"],
            "context": f["context"],
            "keywords": f["keywords"],
            "screenshot": "",
        }
        for f in FINDINGS
    ]
    return {
        "video": VIDEO_NAME,  # basename only; never an absolute path
        "generated_at": GENERATED_AT,
        "summary": summary,
        "findings": findings,
    }


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # 1) JSON report (machine-readable artifact).
    (OUT_DIR / "example_report.json").write_text(
        json.dumps(_build_json(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    # 2) WebVTT transcript (via the product generator).
    (OUT_DIR / "example_transcript.vtt").write_text(
        generate_webvtt(SEGMENTS, language=LANGUAGE),
        encoding="utf-8",
    )

    # 3) Standalone interactive HTML (via the product renderer; video_path=None
    #    => opens from file:// with no embedded video and no server).
    #    static_demo=True bakes the self-contained sample mode: the client skips the
    #    /api/review-state hydration fetch (zero network requests on GitHub Pages)
    #    and the video panel shows an honest empty state instead of a dead player.
    html = render_html_report_pro(
        video_name=VIDEO_NAME,
        video_path=None,
        generated_at=GENERATED_AT,
        executive_summary=EXECUTIVE_SUMMARY,
        findings=FINDINGS,
        segments=SEGMENTS,
        errors=None,
        embed_video=False,
        language=LANGUAGE,
        static_demo=True,
    )
    (OUT_DIR / "example_report.html").write_text(html, encoding="utf-8")

    print("Wrote examples/example_report.json, example_transcript.vtt, example_report.html")


if __name__ == "__main__":
    main()
