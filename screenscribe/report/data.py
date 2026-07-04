"""Shared report primitives and the single console instance."""

import shutil
from pathlib import Path
from typing import Any

from rich.console import Console

from ..detect import Detection
from ..transcribe import Segment

console = Console()


def _prepare_html_video_source(video_path: Path, output_path: Path) -> str:
    """Ensure report can load video via relative path (file:// and http:// friendly)."""
    if not video_path.exists():
        return str(video_path)

    output_dir = output_path.parent
    target_video = output_dir / video_path.name

    if target_video.exists():
        return target_video.name

    try:
        if video_path.resolve() == target_video.resolve():
            return target_video.name
    except OSError:
        # If resolve fails, continue with symlink/copy attempts below.
        pass

    try:
        target_video.symlink_to(video_path.resolve())
    except OSError:
        shutil.copy2(video_path, target_video)

    return target_video.name


def _serialize_transcript_segments(segments: list[Segment] | None) -> list[dict[str, Any]]:
    """Serialize transcript segments for report artifacts."""
    if not segments:
        return []
    return [
        {
            "id": s.id,
            "start": s.start,
            "end": s.end,
            "text": s.text,
        }
        for s in segments
    ]


def _format_timestamped_transcript(segments: list[Segment] | None) -> str:
    """Format transcript into stable timestamped lines."""
    if not segments:
        return ""
    return "\n".join(
        f"[{segment.start:.1f}s - {segment.end:.1f}s] {segment.text}" for segment in segments
    )


def _build_unified_findings_lookup(
    unified_findings: list[Any] | None,
) -> dict[tuple[int, float], Any]:
    """Index unified findings by their primary and merged detection keys.

    Keyed on the composite ``(detection_id, timestamp)`` (merged-aware via
    ``merged_from_ids``). Note: POIs that match no transcript segment all carry
    ``detection_id=0`` upstream; two such findings sharing a timestamp collide
    on this key. For per-screenshot resolution that survives that collision use
    :class:`UnifiedFindingResolver` instead.
    """
    lookup: dict[tuple[int, float], Any] = {}
    for finding in unified_findings or []:
        lookup[(finding.detection_id, finding.timestamp)] = finding
        for detection_id, timestamp in getattr(finding, "merged_from_ids", []):
            lookup[(detection_id, timestamp)] = finding
    return lookup


class UnifiedFindingResolver:
    """Resolve the unified finding for a ``(detection, screenshot_path)`` pair.

    Single source of truth for report writers (JSON/HTML/MD). Resolution order:

    1. By the screenshot the finding was actually analyzed from
       (``finding.screenshot_path``) — disambiguates distinct POIs that share
       ``detection_id=0`` and the same timestamp (BH51/BH55), since each is
       analyzed from its own frame.
    2. By the merged-aware composite key ``(detection_id, timestamp)`` — covers
       merged-away duplicates (which carry no own screenshot_path) and any
       finding whose screenshot_path is unavailable (e.g. text-only backend).
    """

    def __init__(self, unified_findings: list[Any] | None) -> None:
        self._by_key = _build_unified_findings_lookup(unified_findings)
        # Map a resolved screenshot path -> finding. Distinct paths are unique
        # per finding; a None/missing path is simply absent (falls back to key).
        self._by_path: dict[str, Any] = {}
        for finding in unified_findings or []:
            path = getattr(finding, "screenshot_path", None)
            if path is not None:
                self._by_path[str(path)] = finding

    def resolve(self, detection: Any, screenshot_path: Any | None = None) -> Any | None:
        """Return the finding for this detection/screenshot, or ``None``."""
        if screenshot_path is not None:
            hit = self._by_path.get(str(screenshot_path))
            if hit is not None:
                return hit
        return self._by_key.get((detection.segment.id, detection.segment.start))

    def is_anchor_frame(self, detection: Any, screenshot_path: Any | None, finding: Any) -> bool:
        """True when this frame is the survivor (anchor) frame of ``finding``.

        Path-first (BH51/BH55): when the finding carries its own
        ``screenshot_path`` the anchor is the frame whose path matches it, which
        disambiguates distinct POIs sharing ``detection_id=0``. When the finding
        has no path (text-only backend), fall back to the finding's own
        ``(detection_id, timestamp)`` key — the same precedence the resolver uses.
        Merged-away member frames satisfy neither test, so they fold in instead.
        """
        if finding is None:
            return False
        finding_path = getattr(finding, "screenshot_path", None)
        if finding_path is not None:
            return screenshot_path is not None and str(screenshot_path) == str(finding_path)
        return (detection.segment.id, detection.segment.start) == (
            finding.detection_id,
            finding.timestamp,
        )


class FoldedFrame:
    """One report row after the auto-merge fold.

    ``detection``/``screenshot_path`` are the survivor (anchor) frame; ``members``
    are the merged-away evidence frames that fold into it (each itself a
    ``(detection, screenshot_path)`` pair). A row whose finding is a genuine
    analysis miss (or a pre-AI snapshot with no findings) simply has no members.
    """

    __slots__ = ("detection", "members", "screenshot_path")

    def __init__(self, detection: Any, screenshot_path: Any) -> None:
        self.detection = detection
        self.screenshot_path = screenshot_path
        self.members: list[tuple[Any, Any]] = []


def fold_screenshots(
    screenshots: list[tuple[Detection, Path]],
    unified_findings: list[Any] | None,
) -> list[FoldedFrame]:
    """Collapse per-frame screenshots into per-survivor report rows.

    The auto LLM-merge reduces N findings to M survivors (M <= N), recording each
    merged-away duplicate in the survivor's ``merged_from_ids``. The report
    ``findings[]`` must mirror that reduction: one row per survivor, with the
    merged-away frames folded in as evidence members -- aligning the auto-merge
    export with the human-merge fold already done in ``review_app.js`` (G6).

    Partitioning is PATH-FIRST (mirrors :class:`UnifiedFindingResolver`): a frame
    whose path is a survivor's ``screenshot_path`` becomes a row; any other frame
    is folded under the survivor it resolves to via the composite
    ``(detection_id, timestamp)`` key. This survives ``detection_id=0`` collisions
    (BH51/BH55) because the survivor row is identified by frame path, not by the
    shared key. A frame that resolves to no finding (analysis miss / pre-AI
    snapshot) stays as its own row so failed items are never dropped -- keeping
    ``--resume`` retry safe (the checkpoint screenshots stay untouched upstream).
    """
    resolver = UnifiedFindingResolver(unified_findings)

    rows: list[FoldedFrame] = []
    row_by_finding_id: dict[int, FoldedFrame] = {}
    pending_members: list[tuple[Detection, Path, Any]] = []

    for detection, screenshot_path in screenshots:
        finding = resolver.resolve(detection, screenshot_path)
        if finding is None:
            # Genuine analysis miss / pre-AI snapshot -> standalone row.
            rows.append(FoldedFrame(detection, screenshot_path))
        elif resolver.is_anchor_frame(detection, screenshot_path, finding):
            row = FoldedFrame(detection, screenshot_path)
            rows.append(row)
            # ``id()`` keys identity, not equality: distinct survivors never share
            # a row even with colliding detection_id/timestamp.
            row_by_finding_id[id(finding)] = row
        else:
            pending_members.append((detection, screenshot_path, finding))

    for detection, screenshot_path, finding in pending_members:
        survivor_row = row_by_finding_id.get(id(finding))
        if survivor_row is not None:
            survivor_row.members.append((detection, screenshot_path))
        else:
            # Survivor anchor frame absent from screenshots (degenerate): promote
            # the first such member to a standalone row so the finding is never
            # lost; later members fold under it.
            promoted = FoldedFrame(detection, screenshot_path)
            rows.append(promoted)
            row_by_finding_id[id(finding)] = promoted

    return rows


# Operator-visible marker for low-confidence / degraded model output (D7).
# Kept in one place so JSON/HTML/Markdown agree on the exact wording.
DEGRADED_MARKER_LABEL = "UNVERIFIED — degraded model output"


def _is_degraded_analysis(finding: Any | None) -> bool:
    """True when a finding is low-confidence and must be shown as unverified.

    Presentational mirror of ``unified.orchestrator._is_degraded_finding``:
    a finding counts as degraded when ``confidence == "degraded"`` OR it was
    ``parsed_from_unstructured_output`` (recovered from non-JSON model prose).
    A missing finding (``None``) is not "degraded" — it is honestly ``missing``.
    """
    if finding is None:
        return False
    return getattr(finding, "confidence", "high") == "degraded" or bool(
        getattr(finding, "parsed_from_unstructured_output", False)
    )


def _serialize_unified_analysis(finding: Any | None) -> dict[str, Any]:
    """Serialize unified analysis payload for report artifacts."""
    if finding is None:
        return {
            "status": "missing",
            "is_issue": False,
            "degraded": False,
            "confidence": "none",
            "parsed_from_unstructured_output": False,
            "sentiment": "unknown",
            "severity": "none",
            "summary": "",
            "action_items": [],
            "affected_components": [],
            "suggested_fix": "",
            "ui_elements": [],
            "issues_detected": [],
            "accessibility_notes": [],
            "design_feedback": "",
            "technical_observations": "",
            "response_id": None,
            "merged_from_ids": [],
        }

    return {
        "status": "completed",
        "is_issue": finding.is_issue,
        # Derived flag so MD/HTML/JSON consumers do not each re-derive the
        # "degraded OR parsed_from_unstructured_output" predicate (D7).
        "degraded": _is_degraded_analysis(finding),
        "confidence": getattr(finding, "confidence", "high"),
        "parsed_from_unstructured_output": getattr(
            finding, "parsed_from_unstructured_output", False
        ),
        "sentiment": finding.sentiment,
        "severity": finding.severity,
        "summary": finding.summary,
        "action_items": finding.action_items,
        "affected_components": finding.affected_components,
        "suggested_fix": finding.suggested_fix,
        "ui_elements": finding.ui_elements,
        "issues_detected": finding.issues_detected,
        "accessibility_notes": finding.accessibility_notes,
        "design_feedback": finding.design_feedback,
        "technical_observations": finding.technical_observations,
        "response_id": finding.response_id or None,
        # Provenance trail (cut A LLM-merge / cut C routing): the (detection_id,
        # timestamp) pairs of every finding folded into this one, so the report
        # can show what a merged finding was assembled from.
        "merged_from_ids": list(getattr(finding, "merged_from_ids", []) or []),
    }


def _build_analysis_passes(
    detections: list[Detection],
    screenshots: list[tuple[Detection, Path]],
    unified_findings: list[Any] | None,
) -> dict[str, dict[str, Any]]:
    """Summarize which analysis passes populated the current report."""
    resolver = UnifiedFindingResolver(unified_findings)
    matched_count = sum(
        1
        for detection, screenshot_path in screenshots
        if resolver.resolve(detection, screenshot_path) is not None
    )
    unified_total = len(unified_findings or [])

    unified_status = "empty"
    if screenshots and matched_count:
        unified_status = "partial" if matched_count < len(screenshots) else "completed"

    return {
        "detections": {
            "status": "completed" if detections else "empty",
            "count": len(detections),
        },
        "screenshots": {
            "status": "completed" if screenshots else "empty",
            "count": len(screenshots),
        },
        "unified_analysis": {
            "status": unified_status,
            "count": unified_total,
            "matched_findings": matched_count,
            "unmatched_findings": max(len(screenshots) - matched_count, 0),
        },
    }
