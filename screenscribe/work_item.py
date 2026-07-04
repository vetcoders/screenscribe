"""Small common work-item shape for review and analyze surfaces."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

WorkItemSource = Literal["review_detection", "review_manual_frame", "analyze_marker"]

# Per-source `analysis` sub-schema (P2-15).
#
# The `analysis` dict is intentionally NOT a single uniform key-set across
# sources, because the sources carry genuinely different data:
#
#   * `review_detection` (from a UnifiedFinding) is the rich first-pass model:
#     it already classified the finding (is_issue/sentiment), proposed
#     action_items, and recorded UI/accessibility/design observations.
#   * `analyze_marker` / `review_manual_frame` (from a VLM AnalysisResult /
#     ManualFrameResult) are a leaner on-demand probe of one frame; the source
#     dataclass simply has no is_issue/sentiment/action_items/ui_elements/...
#     fields. Emitting those keys as empty placeholders here would fabricate a
#     shape the upstream object never had.
#
# A truly shared serializer would have to live with the dataclasses in
# unified_analysis / analyze_server / review_server (out of this module's
# scope), so the per-source variant is documented and pinned here instead. The
# constants below are the contract; `test_work_item.py` asserts the produced
# key-sets match, turning silent drift into a red test.
DETECTION_ANALYSIS_KEYS: frozenset[str] = frozenset(
    {
        "is_issue",
        "sentiment",
        "severity",
        "summary",
        "action_items",
        "affected_components",
        "suggested_fix",
        "ui_elements",
        "issues_detected",
        "accessibility_notes",
        "design_feedback",
        "technical_observations",
        "response_id",
        "confidence",
        "parsed_from_unstructured_output",
    }
)
# VLM-result sub-schema is a documented SUBSET of the detection schema: every
# key it emits also exists there (no source-unique keys), so consumers reading
# the smaller set stay forward-compatible with the richer one.
RESULT_ANALYSIS_KEYS: frozenset[str] = frozenset(
    {
        "severity",
        "summary",
        "issues_detected",
        "suggested_fix",
        "affected_components",
        "response_id",
    }
)

# The single human-review decision vocabulary. `none` is an explicit string
# ("not reviewed"), never absence-of-key — absence still maps to `none`.
Verdict = Literal["accepted", "rejected", "none"]
VERDICT_VALUES: tuple[str, ...] = ("accepted", "rejected", "none")


def normalize_verdict(value: Any) -> str:
    """Map any legacy/raw decision value onto the verdict vocabulary.

    Legacy migration (loader/migration ONLY): the old boolean `confirmed`
    field is read as ``True -> accepted`` / ``False -> rejected``; anything
    missing/null/unknown is the honest ``none`` ("not reviewed"). New code
    paths never mint accepted/rejected from absence.
    """
    if value in VERDICT_VALUES:
        return str(value)
    # legacy migration: confirmed -> verdict
    if value is True:
        return "accepted"
    if value is False:
        return "rejected"
    return "none"


@dataclass
class WorkItem:
    """Minimal shared shape for persisted review work."""

    id: str
    source: WorkItemSource
    timestamp: float | None = None
    transcript: str = ""
    notes: str = ""
    category: str = ""
    status: str = ""
    frame: dict[str, Any] = field(default_factory=dict)
    analysis: dict[str, Any] = field(default_factory=dict)
    human_review: dict[str, Any] = field(default_factory=dict)
    export_meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable payload."""
        return {
            "id": self.id,
            "source": self.source,
            "timestamp": self.timestamp,
            "transcript": self.transcript,
            "notes": self.notes,
            "category": self.category,
            "status": self.status,
            "frame": dict(self.frame),
            "analysis": dict(self.analysis),
            "human_review": dict(self.human_review),
            "export_meta": dict(self.export_meta),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> WorkItem:
        """Restore a work item from its persisted JSON shape."""
        return cls(
            id=str(payload.get("id", "")),
            source=payload.get("source", "review_detection"),
            timestamp=payload.get("timestamp"),
            transcript=str(payload.get("transcript") or ""),
            notes=str(payload.get("notes") or ""),
            category=str(payload.get("category") or ""),
            status=str(payload.get("status") or ""),
            frame=dict(payload.get("frame") or {}),
            analysis=dict(payload.get("analysis") or {}),
            human_review=dict(payload.get("human_review") or {}),
            export_meta=dict(payload.get("export_meta") or {}),
        )


def _path_value(value: Any) -> str | None:
    if isinstance(value, Path):
        return str(value)
    if value:
        return str(value)
    return None


def from_unified_finding(
    finding: Any,
    *,
    human_review: dict[str, Any] | None = None,
    export_meta: dict[str, Any] | None = None,
) -> WorkItem:
    """Adapt a UnifiedFinding without changing its model.

    Produces the rich ``analysis`` sub-schema (``DETECTION_ANALYSIS_KEYS``); see
    the module note on the intentional per-source variant (P2-15).
    """
    screenshot_path = _path_value(getattr(finding, "screenshot_path", None))
    frame = {"path": screenshot_path} if screenshot_path else {}
    return WorkItem(
        id=str(getattr(finding, "detection_id", "")),
        source="review_detection",
        timestamp=getattr(finding, "timestamp", None),
        category=str(getattr(finding, "category", "")),
        status="processing",
        frame=frame,
        analysis={
            "is_issue": getattr(finding, "is_issue", None),
            "sentiment": getattr(finding, "sentiment", ""),
            "severity": getattr(finding, "severity", ""),
            "summary": getattr(finding, "summary", ""),
            "action_items": list(getattr(finding, "action_items", []) or []),
            "affected_components": list(getattr(finding, "affected_components", []) or []),
            "suggested_fix": getattr(finding, "suggested_fix", ""),
            "ui_elements": list(getattr(finding, "ui_elements", []) or []),
            "issues_detected": list(getattr(finding, "issues_detected", []) or []),
            "accessibility_notes": list(getattr(finding, "accessibility_notes", []) or []),
            "design_feedback": getattr(finding, "design_feedback", ""),
            "technical_observations": getattr(finding, "technical_observations", ""),
            "response_id": getattr(finding, "response_id", ""),
            "confidence": getattr(finding, "confidence", ""),
            "parsed_from_unstructured_output": getattr(
                finding, "parsed_from_unstructured_output", None
            ),
        },
        human_review=human_review or {},
        export_meta=export_meta or {},
    )


def from_manual_frame(
    marker: Any,
    result: Any | None = None,
    *,
    human_review: dict[str, Any] | None = None,
    export_meta: dict[str, Any] | None = None,
) -> WorkItem:
    """Adapt a review-mode manual frame marker plus optional VLM result."""
    return WorkItem(
        id=str(getattr(marker, "marker_id", "")),
        source="review_manual_frame",
        timestamp=getattr(marker, "timestamp", None),
        transcript=str(getattr(marker, "transcript", "") or ""),
        notes=str(getattr(marker, "notes", "") or ""),
        category=str(getattr(result, "category", "manual_capture") if result else "manual_capture"),
        status=str(getattr(marker, "status", "") or ""),
        frame={"base64": getattr(marker, "frame_base64", "")},
        analysis=_result_analysis(result),
        human_review=human_review or {},
        export_meta=export_meta or {},
    )


def from_analyze_marker(
    marker: Any,
    result: Any | None = None,
    *,
    human_review: dict[str, Any] | None = None,
    export_meta: dict[str, Any] | None = None,
) -> WorkItem:
    """Adapt an analyze-mode frame marker plus optional VLM result."""
    frame: dict[str, Any] = {"base64": getattr(marker, "frame_base64", "")}
    frame_path = _path_value(getattr(marker, "frame_path", None))
    if frame_path:
        frame["path"] = frame_path
    for attr, key in (("frame_media_type", "media_type"), ("frame_extension", "extension")):
        value = getattr(marker, attr, None)
        if value:
            frame[key] = value
    return WorkItem(
        id=str(getattr(marker, "marker_id", "")),
        source="analyze_marker",
        timestamp=getattr(marker, "timestamp", None),
        transcript=str(getattr(marker, "transcript", "") or ""),
        notes=str(getattr(marker, "notes", "") or ""),
        category=str(getattr(result, "category", "unknown") if result else "unknown"),
        status=str(getattr(marker, "status", "") or ""),
        frame=frame,
        analysis=_result_analysis(result),
        human_review=human_review or {},
        export_meta=export_meta or {},
    )


def _result_analysis(result: Any | None) -> dict[str, Any]:
    """VLM-result ``analysis`` sub-schema (``RESULT_ANALYSIS_KEYS``).

    A documented subset of the detection schema — see the module note on the
    per-source variant (P2-15). ``None`` (no VLM result yet) yields ``{}``.
    """
    if result is None:
        return {}
    return {
        "severity": getattr(result, "severity", ""),
        "summary": getattr(result, "summary", ""),
        "issues_detected": list(getattr(result, "issues_detected", []) or []),
        "suggested_fix": getattr(result, "suggested_fix", ""),
        "affected_components": list(getattr(result, "affected_components", []) or []),
        "response_id": getattr(result, "response_id", ""),
    }
