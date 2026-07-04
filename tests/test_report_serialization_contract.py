"""Contract-as-test for the single report serialization key-set (SYS-2).

The same logical object (``UnifiedFinding.analysis``) is serialized by JSON,
HTML and Markdown report writers. Historically each writer hand-rolled its own
subset of keys (json=16, html=12, markdown=lookup-by-id-only), so the artifacts
disagreed for one identical run (P2-14, P3-15, BH5, BH6, BH22, BH55).

These tests pin ONE key-set and ONE merged-aware lookup. A revert in any writer
back to a divergent hand-rolled dict or a narrow ``{detection_id: f}`` lookup
must turn these red.
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
from screenscribe.report.data import UnifiedFindingResolver, _serialize_unified_analysis
from screenscribe.transcribe import Segment
from screenscribe.unified_analysis import UnifiedFinding

# The single source of truth: the key-set produced by the shared serializer.
CANONICAL_KEYS = frozenset(_serialize_unified_analysis(None).keys())


def _detection(seg_id: int, start: float, *, category: str = "bug", text: str = "x") -> Detection:
    return Detection(
        segment=Segment(id=seg_id, start=start, end=start + 2.0, text=text),
        category=category,
        keywords_found=["semantic:bug"],
        context="context",
    )


def _finding(
    detection: Detection,
    *,
    summary: str = "Problem",
    severity: str = "high",
    merged_from_ids: list[tuple[int, float]] | None = None,
) -> UnifiedFinding:
    finding = UnifiedFinding(
        detection_id=detection.segment.id,
        screenshot_path=None,
        timestamp=detection.segment.start,
        category=detection.category,
        is_issue=True,
        sentiment="problem",
        severity=severity,
        summary=summary,
        action_items=["Fix it"],
        affected_components=["CTA"],
        suggested_fix="Fix handler",
        ui_elements=["button"],
        issues_detected=["broken"],
        accessibility_notes=["contrast"],
        design_feedback="tighten",
        technical_observations="race condition",
        response_id="resp_123",
    )
    if merged_from_ids:
        finding.merged_from_ids = merged_from_ids
    return finding


def test_serialized_analysis_carries_merged_from_ids_provenance() -> None:
    """A server-side merged finding (cut A LLM-merge) keeps its merged_from_ids
    provenance in the serialized report payload — so the report can show what a
    folded finding was merged from. A non-merged finding carries an empty list."""
    merged = _finding(_detection(1, 10.0), merged_from_ids=[(2, 20.0), (3, 30.0)])
    payload = _serialize_unified_analysis(merged)
    assert "merged_from_ids" in payload
    assert payload["merged_from_ids"] == [[2, 20.0], [3, 30.0]] or payload["merged_from_ids"] == [
        (2, 20.0),
        (3, 30.0),
    ]

    plain = _serialize_unified_analysis(_finding(_detection(4, 40.0)))
    assert plain["merged_from_ids"] == []
    # The "missing" payload also exposes the key for a stable contract.
    assert _serialize_unified_analysis(None)["merged_from_ids"] == []


def test_render_finding_shows_merged_from_trace() -> None:
    """A merged finding renders a visible 'merged from' trace in the report card;
    an unmerged finding renders none."""
    from screenscribe.html_pro.renderer import _render_finding

    merged_finding = {
        "id": 1,
        "category": "ui",
        "timestamp_formatted": "01:15",
        "timestamp": 75.0,
        "text": "save broken",
        "unified_analysis": {
            "summary": "Save button unresponsive",
            "severity": "high",
            "merged_from_ids": [[2, 20.0], [3, 30.0]],
        },
    }
    merged_html = _render_finding(merged_finding, 1, "en")
    assert "Merged from" in merged_html
    assert "2" in merged_html  # absorbed-finding count

    plain_finding = dict(merged_finding)
    plain_finding["unified_analysis"] = {
        "summary": "Save button unresponsive",
        "severity": "high",
        "merged_from_ids": [],
    }
    plain_html = _render_finding(plain_finding, 1, "en")
    assert "Merged from" not in plain_html


def test_serialized_unified_analysis_has_full_engineering_key_set() -> None:
    """The canonical key-set MUST keep the engineering + traceability fields the
    HTML writer used to drop (sentiment, technical_observations, response_id,
    status)."""
    for required in (
        "status",
        "sentiment",
        "technical_observations",
        "response_id",
        "is_issue",
        "severity",
        "summary",
        "action_items",
        "affected_components",
        "suggested_fix",
        "ui_elements",
        "issues_detected",
        "accessibility_notes",
        "design_feedback",
        "confidence",
        "parsed_from_unstructured_output",
    ):
        assert required in CANONICAL_KEYS, required


def test_json_html_markdown_share_one_unified_analysis_key_set(tmp_path: Path) -> None:
    """JSON, HTML and Markdown emit the SAME unified_analysis key-set for one run."""
    detection = _detection(1, 12.5)
    screenshot = tmp_path / "shot.png"
    screenshot.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    findings = [_finding(detection)]

    json_out = tmp_path / "r.json"
    save_enhanced_json_report(
        detections=[detection],
        screenshots=[(detection, screenshot)],
        video_path=tmp_path / "v.mov",
        output_path=json_out,
        unified_findings=findings,
    )
    json_keys = set(json.loads(json_out.read_text("utf-8"))["findings"][0]["unified_analysis"])

    html_out = tmp_path / "r.html"
    save_html_report_pro(
        detections=[detection],
        screenshots=[(detection, screenshot)],
        video_path=tmp_path / "v.mov",
        output_path=html_out,
        unified_findings=findings,
    )
    html_text = html_out.read_text("utf-8")
    # Extract the embedded findings JSON the viewer consumes.
    match = re.search(r'<script id="original-findings"[^>]*>(.*?)</script>', html_text, re.DOTALL)
    assert match, "HTML report must embed original-findings JSON"
    html_findings = json.loads(match.group(1))
    html_keys = set(html_findings[0]["unified_analysis"])

    assert json_keys == CANONICAL_KEYS
    assert html_keys == CANONICAL_KEYS


def test_html_does_not_resurrect_merged_away_duplicate_as_bare_medium(tmp_path: Path) -> None:
    """BH5/BH55: a kept merged-away screenshot must attribute to the merged
    finding, NOT reappear as a fabricated bare 'medium' issue."""
    primary = _detection(7, 10.0, text="primary issue")
    dup = _detection(8, 40.0, text="duplicate transcript text")
    primary_shot = tmp_path / "p.png"
    dup_shot = tmp_path / "d.png"
    for shot in (primary_shot, dup_shot):
        shot.write_bytes(b"\x89PNG\r\n\x1a\nfake")

    merged = _finding(primary, summary="merged analysis", severity="high")
    merged.merged_from_ids = [(dup.segment.id, dup.segment.start)]

    html_out = tmp_path / "r.html"
    save_html_report_pro(
        detections=[primary, dup],
        screenshots=[(primary, primary_shot), (dup, dup_shot)],
        video_path=tmp_path / "v.mov",
        output_path=html_out,
        unified_findings=[merged],
    )
    match = re.search(
        r'<script id="original-findings"[^>]*>(.*?)</script>',
        html_out.read_text("utf-8"),
        re.DOTALL,
    )
    assert match
    findings = json.loads(match.group(1))
    # Both screenshots resolve to the SAME merged analysis; neither is a bare
    # fabricated 'medium'/'none-confidence' fallback carrying raw transcript text.
    for f in findings:
        ua = f["unified_analysis"]
        assert ua["summary"] == "merged analysis"
        assert ua["status"] == "completed"
        assert "duplicate transcript text" not in ua["summary"]


def test_markdown_does_not_resurrect_merged_away_duplicate(tmp_path: Path) -> None:
    """BH6/BH22: Markdown must use the merged-aware lookup (no narrow id key)."""
    primary = _detection(3, 5.0, text="primary issue")
    dup = _detection(4, 33.0, text="dup transcript noise")
    primary_shot = tmp_path / "p.png"
    dup_shot = tmp_path / "d.png"
    for shot in (primary_shot, dup_shot):
        shot.write_bytes(b"\x89PNG\r\n\x1a\nfake")

    merged = _finding(primary, summary="merged md analysis", severity="high")
    merged.merged_from_ids = [(dup.segment.id, dup.segment.start)]

    md_out = tmp_path / "r.md"
    save_enhanced_markdown_report(
        detections=[primary, dup],
        screenshots=[(primary, primary_shot), (dup, dup_shot)],
        video_path=tmp_path / "v.mov",
        output_path=md_out,
        unified_findings=[merged],
    )
    md = md_out.read_text("utf-8")
    # Both kept screenshots attribute to the SAME merged finding (mirrors the
    # JSON report). The merged-away screenshot must NOT spawn a fabricated issue
    # whose Summary is its own raw transcript text — that was the BH6 bug
    # (narrow {detection_id: f} lookup falling through to the fallback branch).
    assert "merged md analysis" in md
    assert "**Summary:** merged md analysis" in md
    assert "**Summary:** dup transcript noise" not in md


def test_markdown_id_zero_collision_does_not_collapse_findings(tmp_path: Path) -> None:
    """BH22: two POIs with detection_id=0 but different timestamps keep their own
    analysis (composite key, not narrow {0: f})."""
    first = _detection(0, 11.0, text="first poi")
    second = _detection(0, 55.0, text="second poi")
    first_shot = tmp_path / "a.png"
    second_shot = tmp_path / "b.png"
    for shot in (first_shot, second_shot):
        shot.write_bytes(b"\x89PNG\r\n\x1a\nfake")

    f1 = _finding(first, summary="ALPHA summary", severity="high")
    f2 = _finding(second, summary="BRAVO summary", severity="low")

    md_out = tmp_path / "r.md"
    save_enhanced_markdown_report(
        detections=[first, second],
        screenshots=[(first, first_shot), (second, second_shot)],
        video_path=tmp_path / "v.mov",
        output_path=md_out,
        unified_findings=[f1, f2],
    )
    md = md_out.read_text("utf-8")
    # Both distinct analyses survive — neither overwrites the other.
    assert "ALPHA summary" in md
    assert "BRAVO summary" in md


def test_json_id_zero_same_timestamp_collision_disambiguated_by_screenshot(
    tmp_path: Path,
) -> None:
    """BH51: two POIs with detection_id=0 AND identical timestamp_start still get
    their own analysis, disambiguated by the distinct screenshot they were
    analyzed from (the composite (id, timestamp) key alone collides)."""
    first = _detection(0, 7.0, text="alpha moment")
    second = _detection(0, 7.0, text="bravo moment")
    first_shot = tmp_path / "alpha.png"
    second_shot = tmp_path / "bravo.png"
    for shot in (first_shot, second_shot):
        shot.write_bytes(b"\x89PNG\r\n\x1a\nfake")

    f1 = _finding(first, summary="ALPHA analysis", severity="high")
    f1.screenshot_path = first_shot
    f2 = _finding(second, summary="BRAVO analysis", severity="low")
    f2.screenshot_path = second_shot

    json_out = tmp_path / "r.json"
    save_enhanced_json_report(
        detections=[first, second],
        screenshots=[(first, first_shot), (second, second_shot)],
        video_path=tmp_path / "v.mov",
        output_path=json_out,
        unified_findings=[f1, f2],
    )
    report = json.loads(json_out.read_text("utf-8"))
    summaries = {f["unified_analysis"]["summary"] for f in report["findings"]}
    # Both distinct analyses survive — the id=0 + same-timestamp collision does
    # not collapse them to one last-wins finding.
    assert summaries == {"ALPHA analysis", "BRAVO analysis"}


def test_resolver_path_match_on_real_shape_and_safe_fallback() -> None:
    """GATE-1: _by_path is not happy-path-only.

    In the live pipeline a finding's ``screenshot_path`` IS the same path object
    the report later iterates in ``screenshots`` (orchestrator passes the
    screenshots-list path into the finding; checkpoint serializes both with the
    same ``str(path)``), so the str keys always match. This pins that the
    screenshot-path resolution genuinely disambiguates the BH51 shape (two POIs
    with detection_id=0 + identical timestamp, analyzed from different frames),
    and that a path-representation mismatch degrades to the composite-key
    fallback without crashing or mis-resolving a unique key.
    """
    det0 = _detection(0, 5.0, text="alpha")
    path_a = Path("out/screenshots/01_alpha_00-05.jpg")
    path_b = Path("out/screenshots/02_beta_00-05.jpg")
    fa = _finding(det0, summary="ALPHA finding")
    fa.screenshot_path = path_a
    fb = _finding(det0, summary="BETA finding")
    fb.screenshot_path = path_b

    resolver = UnifiedFindingResolver([fa, fb])
    # Same representation the writer iterates -> each screenshot resolves to ITS
    # OWN finding; the id=0 + same-timestamp pair does NOT collide.
    assert resolver.resolve(det0, path_a) is fa
    assert resolver.resolve(det0, path_b) is fb

    # Abs-vs-rel divergence cannot happen in-pipeline (same source), but guard it:
    # _by_path misses, resolver falls back to the composite key. With a unique
    # (id, timestamp) the fallback still resolves correctly — no crash, no loss.
    solo = _finding(_detection(7, 12.0), summary="solo")
    solo.screenshot_path = Path("/abs/out/07_solo_00-12.jpg")
    resolver2 = UnifiedFindingResolver([solo])
    assert resolver2.resolve(_detection(7, 12.0), Path("out/07_solo_00-12.jpg")) is solo
    assert resolver2.resolve(_detection(7, 12.0), None) is solo
