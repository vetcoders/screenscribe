"""G6b: auto-LLM-merge reduction must reach the saved report.

These tests reproduce the real-world "10 -> 7 findings" terminal log that did
NOT survive into ``review_report.json`` (members stayed in ``findings[]``, merely
tagged with ``merged_from_ids``). The fold lives purely in the report writers:
report rows = survivor findings, merged-away members fold in as evidence frames.

Fixtures are SYNTHETIC and neutral — no private recording content is copied.
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
from screenscribe.transcribe import Segment
from screenscribe.unified_analysis import UnifiedFinding


def _detection(idx: int, *, category: str = "bug", text: str | None = None) -> Detection:
    return Detection(
        segment=Segment(
            id=idx,
            start=float(idx),
            end=float(idx) + 0.5,
            text=text or f"Finding number {idx} described by the narrator.",
        ),
        category=category,
        keywords_found=[f"semantic:{category}"],
        context=f"Context for synthetic finding {idx}.",
    )


def _finding(
    detection: Detection,
    screenshot_path: Path | None,
    *,
    is_issue: bool = True,
    severity: str = "high",
    merged_from_ids: list[tuple[int, float]] | None = None,
) -> UnifiedFinding:
    return UnifiedFinding(
        detection_id=detection.segment.id,
        screenshot_path=screenshot_path,
        timestamp=detection.segment.start,
        category=detection.category,
        is_issue=is_issue,
        sentiment="problem" if is_issue else "positive",
        severity=severity if is_issue else "none",
        summary=f"Survivor summary {detection.segment.id}",
        action_items=[f"Fix item {detection.segment.id}"],
        affected_components=[f"component-{detection.segment.id}"],
        suggested_fix="Apply the fix",
        ui_elements=[],
        issues_detected=[],
        accessibility_notes=[],
        design_feedback="",
        technical_observations="",
        response_id=f"resp_{detection.segment.id}",
        merged_from_ids=merged_from_ids or [],
    )


def _build_10_to_7(
    tmp_path: Path,
) -> tuple[
    list[Detection],
    list[tuple[Detection, Path]],
    list[UnifiedFinding],
]:
    """10 screenshots collapse to 7 survivors (3 members merged away).

    Survivors 1..5 stand alone. Survivor 6 absorbs member 8. Survivor 7 absorbs
    members 9 and 10. Members carry no own survivor screenshot_path; they resolve
    to their survivor via the (detection_id, timestamp) provenance key.
    """
    detections = [_detection(i) for i in range(1, 11)]
    shots: list[Path] = []
    for i in range(1, 11):
        p = tmp_path / f"shot_{i:02d}.png"
        p.write_bytes(b"fake-frame")
        shots.append(p)
    screenshots = list(zip(detections, shots, strict=True))

    findings: list[UnifiedFinding] = []
    for i in range(5):  # survivors 1..5 stand alone
        findings.append(_finding(detections[i], shots[i]))
    # survivor 6 (index 5) absorbs member 8 (index 7)
    findings.append(
        _finding(
            detections[5],
            shots[5],
            merged_from_ids=[(detections[7].segment.id, detections[7].segment.start)],
        )
    )
    # survivor 7 (index 6) absorbs members 9 and 10 (indices 8, 9)
    findings.append(
        _finding(
            detections[6],
            shots[6],
            merged_from_ids=[
                (detections[8].segment.id, detections[8].segment.start),
                (detections[9].segment.id, detections[9].segment.start),
            ],
        )
    )
    return detections, screenshots, findings


def test_json_report_folds_auto_merge_10_to_7(tmp_path: Path) -> None:
    """Real 10 -> 7 reproduction. RED before fix: len(findings)==10."""
    detections, screenshots, findings = _build_10_to_7(tmp_path)
    output = tmp_path / "report.json"

    save_enhanced_json_report(
        detections=detections,
        screenshots=screenshots,
        video_path=tmp_path / "demo.mov",
        output_path=output,
        unified_findings=findings,
    )

    data = json.loads(output.read_text(encoding="utf-8"))

    # Core invariant: report rows == survivors, not frames.
    assert len(data["findings"]) == 7
    assert data["summary"]["total"] == 7
    assert sum(data["severity_breakdown"].values()) <= 7
    # All 7 survivors are issues here -> severity sum == 7.
    assert sum(data["severity_breakdown"].values()) == 7
    assert data["analysis_passes"]["unified_analysis"]["count"] == 7

    # Members are NOT lost: they fold in as evidence under their survivor.
    member_ids: set[int] = set()
    for finding in data["findings"]:
        for frame in finding.get("merged_frames", []):
            member_ids.add(frame["id"])
    assert member_ids == {8, 9, 10}

    # No survivor row carries a merged-away member id as its own row id.
    row_ids = {f["id"] for f in data["findings"]}
    assert row_ids == {1, 2, 3, 4, 5, 6, 7}


def test_markdown_and_html_parity_with_json(tmp_path: Path) -> None:
    """All three writers fold to the same 7 survivor rows."""
    detections, screenshots, findings = _build_10_to_7(tmp_path)

    json_out = tmp_path / "report.json"
    md_out = tmp_path / "report.md"
    html_out = tmp_path / "report.html"

    save_enhanced_json_report(
        detections=detections,
        screenshots=screenshots,
        video_path=tmp_path / "demo.mov",
        output_path=json_out,
        unified_findings=findings,
    )
    save_enhanced_markdown_report(
        detections=detections,
        screenshots=screenshots,
        video_path=tmp_path / "demo.mov",
        output_path=md_out,
        unified_findings=findings,
    )
    save_html_report_pro(
        detections=detections,
        screenshots=screenshots,
        video_path=tmp_path / "demo.mov",
        output_path=html_out,
        unified_findings=findings,
    )

    data = json.loads(json_out.read_text(encoding="utf-8"))
    assert len(data["findings"]) == 7

    md = md_out.read_text(encoding="utf-8")
    # Markdown issue headings: one per survivor row (7), not per frame (10).
    assert md.count("\n### [") == 7

    html = html_out.read_text(encoding="utf-8")
    # The HTML embeds findings JSON; count survivor ids present once.
    for sid in (1, 2, 3, 4, 5, 6, 7):
        assert f'"id": {sid}' in html or f'"id":{sid}' in html


def test_partial_run_keeps_unanalyzed_frames(tmp_path: Path) -> None:
    """Partial run: failed/unanalyzed frames are NOT dropped by the fold.

    7 survivors fold their members, but a frame that resolves to NO unified
    finding (analysis miss) stays as its own row with status='missing'.
    """
    detections, screenshots, findings = _build_10_to_7(tmp_path)
    # Add an 11th detection/screenshot that has NO unified finding (failed item).
    failed_det = _detection(11)
    failed_shot = tmp_path / "shot_11.png"
    failed_shot.write_bytes(b"fake-frame")
    detections.append(failed_det)
    screenshots.append((failed_det, failed_shot))

    output = tmp_path / "report.json"
    save_enhanced_json_report(
        detections=detections,
        screenshots=screenshots,
        video_path=tmp_path / "demo.mov",
        output_path=output,
        unified_findings=findings,
    )

    data = json.loads(output.read_text(encoding="utf-8"))
    # 7 survivors + 1 unanalyzed failed frame = 8 rows; members 8,9,10 folded.
    assert len(data["findings"]) == 8
    row_ids = {f["id"] for f in data["findings"]}
    assert 11 in row_ids
    assert {8, 9, 10}.isdisjoint(row_ids)
    failed_row = next(f for f in data["findings"] if f["id"] == 11)
    assert failed_row["unified_analysis"]["status"] == "missing"


def test_detection_id_collision_path_first_fold(tmp_path: Path) -> None:
    """Path-first fold: two POIs sharing detection_id=0 merge into DIFFERENT
    survivors without over-drop or mis-bucket."""
    # Two distinct POI frames both carry detection_id=0 and the SAME timestamp.
    poi_a = Detection(
        segment=Segment(id=0, start=5.0, end=5.5, text="POI A unmatched segment"),
        category="ui",
        keywords_found=["poi"],
        context="poi a",
    )
    poi_b = Detection(
        segment=Segment(id=0, start=5.0, end=5.5, text="POI B unmatched segment"),
        category="ui",
        keywords_found=["poi"],
        context="poi b",
    )
    surv_a = _detection(1)
    surv_b = _detection(2)

    shot_a = tmp_path / "shot_poi_a.png"
    shot_b = tmp_path / "shot_poi_b.png"
    shot_surv_a = tmp_path / "shot_surv_a.png"
    shot_surv_b = tmp_path / "shot_surv_b.png"
    for p in (shot_a, shot_b, shot_surv_a, shot_surv_b):
        p.write_bytes(b"fake-frame")

    # Survivor A absorbs POI A; survivor B absorbs POI B. Both POIs share key
    # (0, 5.0); only path-first resolution attributes each to the right survivor.
    f_a = _finding(surv_a, shot_surv_a, merged_from_ids=[(0, 5.0)])
    f_b = _finding(surv_b, shot_surv_b, merged_from_ids=[(0, 5.0)])

    detections = [surv_a, poi_a, surv_b, poi_b]
    screenshots = [
        (surv_a, shot_surv_a),
        (poi_a, shot_a),
        (surv_b, shot_surv_b),
        (poi_b, shot_b),
    ]
    output = tmp_path / "report.json"
    save_enhanced_json_report(
        detections=detections,
        screenshots=screenshots,
        video_path=tmp_path / "demo.mov",
        output_path=output,
        unified_findings=[f_a, f_b],
    )

    data = json.loads(output.read_text(encoding="utf-8"))
    # 2 survivors, each keeping its own frame; both POI frames folded as members.
    assert len(data["findings"]) == 2
    row_ids = {f["id"] for f in data["findings"]}
    assert row_ids == {1, 2}
    # Each survivor keeps its OWN screenshot (no over-drop of the survivor frame).
    by_id = {f["id"]: f for f in data["findings"]}
    assert by_id[1]["screenshot"] == "shot_surv_a.png"
    assert by_id[2]["screenshot"] == "shot_surv_b.png"


def test_empty_unified_findings_degrades_to_all_screenshots(tmp_path: Path) -> None:
    """Pre-AI snapshot: unified_findings=[] -> writer iterates all screenshots."""
    detections = [_detection(i) for i in range(1, 4)]
    shots = []
    for i in range(1, 4):
        p = tmp_path / f"shot_{i}.png"
        p.write_bytes(b"fake-frame")
        shots.append(p)
    screenshots = list(zip(detections, shots, strict=True))

    output = tmp_path / "report.json"
    save_enhanced_json_report(
        detections=detections,
        screenshots=screenshots,
        video_path=tmp_path / "demo.mov",
        output_path=output,
        unified_findings=[],
    )

    data = json.loads(output.read_text(encoding="utf-8"))
    assert len(data["findings"]) == 3
    assert data["summary"]["total"] == 3


def test_html_merged_member_screenshot_embeds_base64(tmp_path: Path) -> None:
    """Finding 610: merged-evidence thumbnails embed base64 (single-file promise).

    The main finding thumbnails embed the frame inline as a data URI so the
    report stays self-contained. Merged-away member frames must follow the same
    rule: when the member frame exists on disk, its thumbnail renders as a
    ``data:image/...;base64,...`` source rather than a ``screenshots/<file>``
    path ref — otherwise the single-file report leaks an external dependency and
    the thumbnail 404s once the report is moved away from its ``screenshots/``
    directory. (The ``screenshots/<file>`` path is kept in the finding dict as a
    fallback for members whose file is missing; that path is exercised by
    ``test_report_merged_frames_render.py``.)
    """
    detections, screenshots, findings = _build_10_to_7(tmp_path)
    html_out = tmp_path / "report.html"

    save_html_report_pro(
        detections=detections,
        screenshots=screenshots,
        video_path=tmp_path / "demo.mov",
        output_path=html_out,
        unified_findings=findings,
    )

    html = html_out.read_text(encoding="utf-8")
    # Isolate the rendered merged-frame thumbnails from the embedded JSON island.
    thumbs = re.findall(r'<img class="merged-frame-thumb" src="([^"]*)"', html)
    assert thumbs, "no merged-frame thumbnails were rendered"
    for src in thumbs:
        assert src.startswith("data:image/"), (
            f"merged-evidence thumbnail is not self-contained: {src!r}; "
            "expected an embedded 'data:image/...;base64,...' source"
        )
    # With every member frame present on disk, no thumbnail should fall back to
    # an external "screenshots/<file>" reference in the rendered HTML.
    assert 'src="screenshots/' not in html, (
        "rendered HTML still references external screenshots/ paths; "
        "single-file promise broken"
    )
