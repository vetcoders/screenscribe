from pathlib import Path

from screenscribe.analyze_server import AnalysisResult, FrameMarker
from screenscribe.review_server import ManualFrameMarker, ManualFrameResult
from screenscribe.unified_analysis import UnifiedFinding
from screenscribe.work_item import (
    DETECTION_ANALYSIS_KEYS,
    RESULT_ANALYSIS_KEYS,
    WorkItem,
    from_analyze_marker,
    from_manual_frame,
    from_unified_finding,
    normalize_verdict,
)


def test_work_item_roundtrips_unified_finding() -> None:
    finding = UnifiedFinding(
        detection_id=42,
        screenshot_path=Path("screenshots/42.jpg"),
        timestamp=12.5,
        category="bug",
        is_issue=True,
        sentiment="problem",
        severity="high",
        summary="CTA is broken",
        action_items=["Fix click handler"],
        affected_components=["CTA"],
        suggested_fix="Reconnect event listener",
        ui_elements=["button"],
        issues_detected=["click does nothing"],
        accessibility_notes=["missing focus state"],
        design_feedback="",
        technical_observations="handler missing",
        response_id="resp-42",
    )
    item = from_unified_finding(
        finding,
        human_review={"verdict": "accepted", "notes": "real"},
        export_meta={"report": "screen_report.json"},
    )

    restored = WorkItem.from_dict(item.to_dict())

    assert restored.to_dict() == item.to_dict()
    assert restored.source == "review_detection"
    assert restored.status == "processing"
    assert restored.analysis["severity"] == "high"
    assert restored.human_review["verdict"] == "accepted"


def test_analysis_sub_schema_is_a_documented_per_source_variant() -> None:
    """P2-15: the two analysis serializers emit pinned, documented key-sets.

    They are NOT identical by design (the VLM result lacks the rich
    classification fields a UnifiedFinding carries), but the leaner result schema
    must stay a strict SUBSET of the detection schema so consumers reading the
    smaller set never break on the richer one. This guards against silent drift:
    adding/removing a key in either serializer fails here until the documented
    contract constant is updated to match.
    """
    finding = UnifiedFinding(
        detection_id=1,
        screenshot_path=Path("s/1.jpg"),
        timestamp=1.0,
        category="bug",
        is_issue=True,
        sentiment="problem",
        severity="high",
        summary="x",
        action_items=[],
        affected_components=[],
        suggested_fix="",
        ui_elements=[],
        issues_detected=[],
        accessibility_notes=[],
        design_feedback="",
        technical_observations="",
        response_id="r",
    )
    detection_keys = set(from_unified_finding(finding).analysis.keys())

    marker = FrameMarker(marker_id="m1", timestamp=2.0, frame_base64="b")
    result = AnalysisResult(
        marker_id="m1",
        timestamp=2.0,
        category="ui",
        severity="low",
        summary="y",
        issues_detected=[],
        suggested_fix="",
        affected_components=[],
        response_id="r2",
    )
    result_keys = set(from_analyze_marker(marker, result).analysis.keys())

    # Produced shapes match their documented contract constants.
    assert detection_keys == set(DETECTION_ANALYSIS_KEYS)
    assert result_keys == set(RESULT_ANALYSIS_KEYS)
    # The result schema is a strict subset (no source-unique keys).
    assert RESULT_ANALYSIS_KEYS < DETECTION_ANALYSIS_KEYS


def test_normalize_verdict_maps_legacy_and_passes_through() -> None:
    """Legacy migration (loader-only): boolean confirmed -> verdict; absence and
    unknown values collapse to the explicit `none`, never a fail-open accept."""
    # legacy migration: confirmed -> verdict
    assert normalize_verdict(True) == "accepted"
    assert normalize_verdict(False) == "rejected"
    assert normalize_verdict(None) == "none"
    assert normalize_verdict("unknown") == "none"
    # New vocabulary passes through untouched.
    assert normalize_verdict("accepted") == "accepted"
    assert normalize_verdict("rejected") == "rejected"
    assert normalize_verdict("none") == "none"


def test_work_item_roundtrips_review_manual_frame() -> None:
    marker = ManualFrameMarker(
        marker_id="manual-1",
        timestamp=9.25,
        frame_base64="/9j/fake",
        transcript="spoken note",
        notes="operator note",
        status="completed",
    )
    result = ManualFrameResult(
        marker_id="manual-1",
        timestamp=9.25,
        category="manual_capture",
        severity="medium",
        summary="Manual capture summary",
        issues_detected=["copy mismatch"],
        suggested_fix="Adjust copy",
        affected_components=["Header"],
        response_id="resp-manual",
    )
    item = from_manual_frame(
        marker,
        result,
        human_review={"annotations": [{"type": "rect"}]},
    )

    restored = WorkItem.from_dict(item.to_dict())

    assert restored.to_dict() == item.to_dict()
    assert restored.source == "review_manual_frame"
    assert restored.frame["base64"] == "/9j/fake"
    assert restored.analysis["summary"] == "Manual capture summary"


def test_work_item_roundtrips_analyze_marker() -> None:
    marker = FrameMarker(
        marker_id="analyze-1",
        timestamp=4.0,
        frame_base64="iVBORfake",
        transcript="dashboard note",
        notes="looks wrong",
        status="completed",
        frame_path=Path("frames/analyze-1.png"),
        frame_media_type="image/png",
        frame_extension=".png",
    )
    result = AnalysisResult(
        marker_id="analyze-1",
        timestamp=4.0,
        category="ui",
        severity="low",
        summary="Analyze marker summary",
        issues_detected=["minor alignment"],
        suggested_fix="Align card",
        affected_components=["Dashboard card"],
        response_id="resp-analyze",
    )
    item = from_analyze_marker(
        marker,
        result,
        human_review={"severity_override": "medium"},
    )

    restored = WorkItem.from_dict(item.to_dict())

    assert restored.to_dict() == item.to_dict()
    assert restored.source == "analyze_marker"
    assert restored.frame["path"] == "frames/analyze-1.png"
    assert restored.human_review["severity_override"] == "medium"
